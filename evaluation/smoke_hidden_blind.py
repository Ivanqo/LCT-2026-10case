"""Blind hidden smoke: import hidden documents without hidden labels and run inference."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import time
import urllib.error
import urllib.request


STATUSES = [
    "CANDIDATE",
    "NEGATIVE_VERIFIED",
    "MISSING_EVIDENCE",
    "NOT_COMPARABLE",
    "CLARIFICATION_REQUIRED",
    "SUSPICION",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--project-name", default="CASE10 hidden blind smoke")
    parser.add_argument("--object-id", default="OBJ-RECHNIKOV-7-7")
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--max-candidate-rate", type=float, default=0.15)
    parser.add_argument("--poll-timeout-sec", type=float, default=240.0)
    args = parser.parse_args()
    headers = {"Content-Type": "application/json"}

    def request(method: str, path: str, body=None, expected_status: int = 200):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(args.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=900) as response:
                assert response.status == expected_status, (path, response.status, expected_status)
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == expected_status:
                return json.load(exc)
            raise RuntimeError(f"{method} {path}: {exc.code} {exc.read().decode('utf-8', errors='replace')[:2000]}") from exc

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
        project = request("POST", "/projects", {"name": args.project_name, "description": "Hidden blind smoke without labels"})
    project_id = project["id"]

    started = time.monotonic()
    imported = request(
        "POST",
        f"/case10/projects/{project_id}/official-dataset/import",
        {
            "object_ids": [args.object_id],
            "include_hidden": True,
            "include_pages": True,
            "include_annotations": True,
            "include_gold": False,
            "allow_hidden_gold_labels": False,
            "run_processes": False,
        },
    )
    assert imported["objects"] == [args.object_id], imported
    assert imported["matrix_params"] == 132, imported
    assert imported["gold_checks"]["created"] == 0 and imported["gold_checks"]["updated"] == 0, imported["gold_checks"]

    start_requested = time.monotonic()
    process = request("POST", f"/case10/projects/{project_id}/processes", {"object_id": args.object_id})
    start_seconds = time.monotonic() - start_requested
    process, status_poll_seconds, statuses_seen = wait_process_ready(process)
    process_id = process["process_id"]
    groups = request("GET", f"/case10/evidence-groups?project_id={project_id}&process_id={process_id}")
    counts = Counter(row.get("finding_status") for row in groups)
    status_counts = {status: int(counts.get(status, 0)) for status in STATUSES}
    protocol = request("GET", f"/case10/protocols/current?project_id={project_id}&process_id={process_id}")
    submission = request("GET", f"/case10/protocols/{protocol['id']}/submission")
    assert not submission["validation_errors"], submission

    positive_like = status_counts["CANDIDATE"] + status_counts["SUSPICION"]
    candidate_rate = positive_like / max(1, len(groups))
    assert positive_like <= args.max_candidates, {"positive_like": positive_like, "limit": args.max_candidates, "status_counts": status_counts}
    assert candidate_rate <= args.max_candidate_rate, {"candidate_rate": candidate_rate, "limit": args.max_candidate_rate, "status_counts": status_counts}

    print(json.dumps({
        "project_id": project_id,
        "object_id": args.object_id,
        "process_id": process_id,
        "protocol_id": protocol["id"],
        "seconds": round(time.monotonic() - started, 2),
        "start_seconds": round(start_seconds, 3),
        "status_poll_seconds": round(status_poll_seconds, 2),
        "status_transitions": statuses_seen,
        "documents": imported["files_index"],
        "pages": imported["page_index"],
        "annotations": imported["annotations"],
        "gold_checks": imported["gold_checks"],
        "groups": len(groups),
        "status_counts": status_counts,
        "positive_like": positive_like,
        "candidate_rate": candidate_rate,
        "submission_checks": len(submission["submission"]["checks"]),
        "submission_valid": True,
        "f1_evaluated": False,
        "hidden_labels_used": False,
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
