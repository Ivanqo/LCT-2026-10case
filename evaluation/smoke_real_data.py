"""Repeatable local HTTP smoke. Credentials are read from environment, never saved."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from evaluation.fixtures import load_gold_checks_jsonl, gold_checks_to_evaluation_fixture
from evaluation.metrics import evaluate_case10


def _load_localization_adjudications(path: Path = Path("evaluation/audits/localization_conflicts.json")) -> list:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--project-name", default="CASE10 official public P0")
    parser.add_argument("--output", type=Path, default=Path("evaluation/reports/real_data"))
    parser.add_argument("--import-only", action="store_true")
    parser.add_argument("--verify-pages", action="store_true")
    parser.add_argument("--verify-protocol", action="store_true")
    parser.add_argument("--poll-timeout-sec", type=float, default=240.0)
    args = parser.parse_args()
    headers = {"Content-Type": "application/json"}

    def request(method, path, body=None, expected_status=200):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(args.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=600) as response:
                assert response.status == expected_status, (path, response.status, expected_status)
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == expected_status:
                return json.load(exc)
            raise RuntimeError(f"{method} {path}: {exc.code} {exc.read().decode()[:2000]}") from exc

    def save(name, value):
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def wait_process_ready(process: dict) -> tuple[dict, float, list[str]]:
        process_id = process["process_id"]
        statuses = [process.get("status")]
        started = time.monotonic()
        while process.get("status") != "READY":
            if process.get("status") == "FAILED":
                raise AssertionError(process)
            if time.monotonic() - started > args.poll_timeout_sec:
                raise TimeoutError(f"Process {process_id} did not reach READY within {args.poll_timeout_sec}s: {process}")
            time.sleep(0.5)
            process = request("GET", f"/case10/processes/{process_id}/status")
            statuses.append(process.get("status"))
        return process, time.monotonic() - started, statuses

    login = request("POST", "/auth/login", {
        "login": os.getenv("DEFAULT_ADMIN_LOGIN", "admin"),
        "password": os.getenv("DEFAULT_ADMIN_PASSWORD", "Admin123!ChangeMe"),
    })
    headers["Authorization"] = "Bearer " + login["access_token"]
    projects = request("GET", "/projects")
    project = next((row for row in projects if row["name"] == args.project_name), None)
    if project is None:
        project = request("POST", "/projects", {"name": args.project_name, "description": "Official public dataset validation"})
    project_id = project["id"]
    started = time.monotonic()
    imported = request("POST", f"/case10/projects/{project_id}/official-dataset/import", {"run_processes": False})
    save("import.json", {"project_id": project_id, "seconds": time.monotonic() - started, **imported})
    assert imported["matrix_params"] == 132, imported
    for key, count in (("document_manifest", 203), ("files_index", 203), ("page_index", 10146), ("annotations", 30318)):
        assert imported[key]["created"] + imported[key]["updated"] == count, (key, imported[key])
    matrix = request("GET", f"/case10/matrix?project_id={project_id}")
    save("matrix.json", matrix)
    print(json.dumps({"project_id": project_id, "import_seconds": round(time.monotonic() - started, 2), "counts": {k: imported[k] for k in ("matrix_params", "document_manifest", "page_index", "annotations")}}, ensure_ascii=False), flush=True)
    if args.import_only:
        return
    gold_path = Path("learning_data/extracted/train_public_203/data/public_gold_checks.jsonl")
    gold_rows = load_gold_checks_jsonl(gold_path)["rows"]
    localization_adjudications = _load_localization_adjudications()
    summary = []
    for object_id in imported["objects"]:
        started = time.monotonic()
        start_requested = time.monotonic()
        process = request("POST", f"/case10/projects/{project_id}/processes", {"object_id": object_id})
        start_seconds = time.monotonic() - start_requested
        process, status_poll_seconds, statuses_seen = wait_process_ready(process)
        process["id"] = process["process_id"]
        groups = request("GET", f"/case10/evidence-groups?project_id={project_id}&process_id={process['id']}")
        assert len({row["parameter"]["code"] for row in groups if row["parameter"]["code"]}) == 132
        assert all((row.get("delta") or {}).get("source") != "gold_fixture" for row in groups)
        protocol = request("GET", f"/case10/protocols/current?project_id={project_id}&process_id={process['id']}")
        resulting_protocol = protocol
        predictions = request("GET", f"/case10/protocols/{protocol['id']}/evaluation-predictions")
        submission = request("GET", f"/case10/protocols/{protocol['id']}/submission")
        assert not submission["validation_errors"], submission
        save(f"{object_id}.predictions.json", predictions)
        save(f"{object_id}.submission.json", submission)
        fixture = gold_checks_to_evaluation_fixture([row for row in gold_rows if row["object_id"] == object_id])
        metrics = evaluate_case10(fixture, predictions, localization_adjudications=localization_adjudications)
        metrics["evaluation_scope"] = "public_matrix_checks_document_annotation_baseline_no_gold_inference"
        metrics["acceptance_metrics_source"] = "Матрица_параметров_редакция1.1.xlsx::МЕТРИКИ"
        save(f"{object_id}.metrics.json", metrics)
        save(f"{object_id}.protocol.json", protocol)
        save(f"{object_id}.groups.json", groups)
        fragments = [fragment for group in groups for fragment in group["fragments"]]
        assert fragments
        for fragment in fragments:
            assert fragment["file_id"] and fragment["page"] and fragment["page_width"] and fragment["page_height"]
            assert len(fragment["bbox_normalized"]) == 4 and len(fragment["bbox_pdf"]) == 4
            assert all(0 <= v <= 1 for v in fragment["bbox_normalized"])
            x1, y1, x2, y2 = fragment["bbox_normalized"]
            assert x1 < x2 and y1 < y2
        if args.verify_pages:
            fragment = fragments[0]
            req = urllib.request.Request(args.base_url + f"/case10/evidence-fragments/{fragment['id']}/page.png", headers=headers)
            with urllib.request.urlopen(req, timeout=60) as response:
                image = response.read()
            assert image.startswith(b"\x89PNG\r\n\x1a\n")
            (args.output / f"{object_id}.evidence.png").write_bytes(image)
        if args.verify_protocol:
            assert len([key for key in protocol["payload"]["annex_2"] if key.startswith("section_")]) == 7
            group = next(group for group in groups if group["fragments"])
            decision_url = f"/case10/evidence-groups/{group['id']}/decisions"
            request("POST", decision_url, {"decision": "Clarification Required", "comment": "Automated workflow smoke: original values require inspector review"})
            latest = request("GET", f"/case10/protocols/current?project_id={project_id}&process_id={process['id']}")
            assert latest["version"] == protocol["version"] + 1
            request("POST", f"/case10/protocols/{protocol['id']}/finalize", expected_status=409)
            finalized = request("POST", f"/case10/protocols/{latest['id']}/finalize")
            assert finalized["status"] == finalized["payload"]["status"] == "FINALIZED"
            resulting_protocol = finalized
            request("POST", decision_url, {"decision": "Confirm"}, expected_status=409)
            request("POST", f"/case10/processes/{process['id']}/run", expected_status=409)
            history = request("GET", f"/case10/processes/{process['id']}/protocols")
            assert len(history) == 2 and history[0]["status"] == "DRAFT"
            audit = request("GET", f"/case10/processes/{process['id']}/audit")
            assert any(row["action"] == "INSPECTOR_DECISION" and row["user_id"] for row in audit)
            save(f"{object_id}.finalized.json", finalized)
            save(f"{object_id}.audit.json", audit)
            req = urllib.request.Request(args.base_url + f"/case10/protocols/{latest['id']}/pdf", headers=headers)
            with urllib.request.urlopen(req, timeout=60) as response:
                pdf = response.read()
            assert pdf.startswith(b"%PDF-")
            (args.output / f"{object_id}.protocol.pdf").write_bytes(pdf)
        result = {
            "object_id": object_id,
            "process_id": process["id"],
            "protocol_id": resulting_protocol["id"],
            "protocol_version": resulting_protocol["version"],
            "protocol_status": resulting_protocol["status"],
            "seconds": round(time.monotonic() - started, 2),
            "start_seconds": round(start_seconds, 3),
            "status_poll_seconds": round(status_poll_seconds, 2),
            "status_transitions": statuses_seen,
            "statuses": dict(Counter(g["finding_status"] for g in groups)),
            "fragments": len(fragments),
            "submission_valid": True,
            "coverage": metrics["coverage"],
            "finding_f1": metrics["finding_f1"],
            "false_positive_rate": metrics["false_positive_rate"],
            "source_localization_exact_file_page": metrics["source_localization_exact_file_page"],
            "evidence_localization_accuracy": metrics["evidence_localization_accuracy"],
            "evidence_localization_accuracy_source_verified": metrics["evidence_localization_accuracy_source_verified"],
            "normalized_value_and_status_accuracy": metrics["normalized_value_and_status_accuracy"],
            "object_level_split_valid": metrics["object_level_split_valid"],
            "abstention_rate": metrics["abstention_rate"],
        }
        summary.append(result)
        save("summary.json", summary)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
