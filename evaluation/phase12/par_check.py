"""Compare official host-import and server-package CLI results for TYU and ALT79B.

The script is diagnostic only: the host route imports the public manifest/indexes,
while the CLI route consumes the explicitly staged package and its registry. It
prints route inputs, finding tuples, output hashes and their differences; it never
uses organizer labels or expected findings to produce a result.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OBJECTS = {"TYU": "tyu", "ALT79B": "alt79b"}


def _env(demo_home: Path, workers: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join((str(ROOT / "api_service"), str(ROOT))),
        "CASE10_LLM_VERIFIER_ENABLED": "0", "CASE10_EXPLICATION_COMPARE": "1",
        "CASE10_ROOM_DRAWING_COMPARE_ENABLED": "1", "CASE10_FREE_HYPOTHESES": "1",
        "CASE10_APPROVAL_UNKNOWN_POLICY": "soft",
        "CASE10_LIVE_TAGGER_WORKERS": str(workers),
        "CASE10_LIVE_TAGGER_CACHE_DIR": env.get("CASE10_LIVE_TAGGER_CACHE_DIR", str(demo_home / "cache")),
    })
    env.pop("CASE10_ORIGINALS_ROOT", None)
    env.pop("CASE10_PARTICIPANT_DATA", None)
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, float, str]:
    started = time.monotonic()
    try:
        proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, check=False)
        return proc.returncode, round(time.monotonic() - started, 2), (proc.stdout + "\n" + proc.stderr)[-6000:]
    except subprocess.TimeoutExpired as exc:
        tail = "\n".join(str(part) for part in (exc.stdout, exc.stderr) if part is not None)[-6000:]
        return 124, round(time.monotonic() - started, 2), f"TIMEOUT after {timeout}s\n{tail}"


def _finding_tuples(payload: dict[str, Any]) -> list[list[str]]:
    rows = []
    for check in payload.get("checks") or []:
        code = str(check.get("matrix_code") or check.get("parameter_code") or "")
        if code not in {"M-078", "M-079", "IOS4-078", "IOS4-079"}:
            continue
        tier = str(check.get("evidence_tier") or "")
        if tier not in {"VIOLATION_CANDIDATE", "SUGGESTION"}:
            continue
        rows.append(["M-078" if code.endswith("078") else "M-079",
                     str(check.get("location") or ""), tier])
    return sorted(rows)


def _read_result(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _summary(path: Path) -> dict[str, Any]:
    payload = _read_result(path)
    package = payload.get("package") or {}
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "checks": len(payload.get("checks") or []),
        "findings": _finding_tuples(payload),
        "finding_counts": dict(sorted(Counter(row[2] for row in _finding_tuples(payload)).items())),
        "package": {key: package[key] for key in (
            "registry_status", "registry_status_reason", "files_total", "files_imported", "documents_by_stage",
            "registry_rows_without_file", "unrecognized_columns", "registry_warnings",
        ) if key in package},
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-home", type=Path, default=os.getenv("CASE10_DEMO_HOME"),
                        help="home previously populated with `deploy/demo/demo.py stage tyu alt79b`")
    parser.add_argument("--host-results-dir", type=Path,
                        help="reuse completed host result files from this directory instead of rerunning the host path")
    parser.add_argument("--out", type=Path, required=True, help="output directory for both route results")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=1500)
    parser.add_argument("--objects", nargs="+", choices=tuple(OBJECTS), default=list(OBJECTS))
    args = parser.parse_args()
    if args.demo_home is None:
        parser.error("set CASE10_DEMO_HOME or pass --demo-home")
    demo_home = args.demo_home.resolve()
    if not demo_home.is_dir():
        parser.error(f"demo home not found: {demo_home}; stage the demo packages first")
    args.out.mkdir(parents=True, exist_ok=True)
    host_dir = args.host_results_dir.resolve() if args.host_results_dir else args.out / "host"
    cli_dir = args.out / "cli"
    if not args.host_results_dir:
        host_dir.mkdir(exist_ok=True)
    cli_dir.mkdir(exist_ok=True)
    env = _env(demo_home, args.workers)
    objects = args.objects
    host_seconds = None
    if args.host_results_dir:
        missing = [key for key in objects if not (host_dir / f"{key}.submission.json").is_file()]
        if missing:
            parser.error(f"host result files missing from {host_dir}: {', '.join(missing)}")
    else:
        host_cmd = [sys.executable, str(ROOT / "evaluation" / "phase12" / "run_baseline.py"),
                    "--mode", "live", "--out", str(host_dir), "--code-root", str(ROOT),
                    "--workers", str(args.workers), *objects]
        host_rc, host_seconds, host_log = _run(host_cmd, cwd=ROOT, env=env, timeout=args.timeout_seconds)
        (args.out / "host.log").write_text(host_log, encoding="utf-8")
        if host_rc != 0:
            print(json.dumps({"host_exit": host_rc, "host_seconds": host_seconds,
                              "log": str(args.out / "host.log")}, ensure_ascii=False, indent=2))
            return 1

    report: dict[str, Any] = {"branch_root": str(ROOT), "host_results_dir": str(host_dir),
                              "host_reused": bool(args.host_results_dir), "host_seconds": host_seconds,
                              "objects": {}}
    report_path = args.out / "parity.json"
    _write_report(report_path, report)
    for object_key in objects:
        package_key = OBJECTS[object_key]
        docs = demo_home / "packages" / package_key
        registry = demo_home / "packages" / f"{package_key}.registry.csv"
        cli_output = cli_dir / package_key / "result.json"
        cli_timings = cli_dir / package_key / "timings.json"
        cli_output.parent.mkdir(parents=True, exist_ok=True)
        if not docs.is_dir() or not registry.is_file():
            print(f"missing staged package/registry for {package_key}: {docs} / {registry}", file=sys.stderr)
            return 1
        cli_cmd = [sys.executable, "-m", "app.cli", "run", "--docs", str(docs), "--registry", str(registry),
                   "--out", str(cli_output), "--timings", str(cli_timings), "--workers", str(args.workers)]
        cli_rc, cli_seconds, cli_log = _run(cli_cmd, cwd=ROOT, env=env, timeout=args.timeout_seconds)
        (cli_output.parent / "run.log").write_text(cli_log, encoding="utf-8")
        if cli_rc != 0 or not cli_output.is_file():
            print(json.dumps({"object": object_key, "cli_exit": cli_rc, "cli_seconds": cli_seconds,
                              "log": str(cli_output.parent / "run.log")}, ensure_ascii=False, indent=2))
            return 1
        host_result = host_dir / f"{object_key}.submission.json"
        host_summary = _summary(host_result)
        cli_summary = _summary(cli_output)
        expected_comparison = None
        if object_key == "TYU":
            expected_path = ROOT / "deploy" / "demo" / "expected" / "tyu.json"
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
            expected_items = sorted([list(map(str, row)) for row in expected.get("items") or []])
            expected_comparison = {
                "items": expected_items,
                "cli_matches_expected": cli_summary["findings"] == expected_items,
                "cli_only": sorted(set(map(tuple, cli_summary["findings"])) - set(map(tuple, expected_items))),
                "expected_only": sorted(set(map(tuple, expected_items)) - set(map(tuple, cli_summary["findings"]))),
            }
        host_run_path = host_dir / f"{object_key}.run.json"
        host_run = json.loads(host_run_path.read_text(encoding="utf-8")) if host_run_path.is_file() else {}
        report["objects"][object_key] = {
            "host": host_summary,
            "host_import": host_run.get("import"),
            "host_run_process_seconds": host_run.get("run_process_seconds"),
            "cli": cli_summary,
            "cli_seconds": cli_seconds,
            "same_findings": host_summary["findings"] == cli_summary["findings"],
            "host_only": sorted(set(map(tuple, host_summary["findings"])) - set(map(tuple, cli_summary["findings"]))),
            "cli_only": sorted(set(map(tuple, cli_summary["findings"])) - set(map(tuple, host_summary["findings"]))),
            "expected_comparison": expected_comparison,
        }
        _write_report(report_path, report)
        print(json.dumps({"object": object_key, "report": str(report_path),
                          "same_findings": report["objects"][object_key]["same_findings"],
                          "host_only": report["objects"][object_key]["host_only"],
                          "cli_only": report["objects"][object_key]["cli_only"]},
                         ensure_ascii=False), flush=True)
    print(json.dumps({"report": str(report_path), "objects": report["objects"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
