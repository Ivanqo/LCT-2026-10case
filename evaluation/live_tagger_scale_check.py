"""CASE10 E1 -- live tagger scale + determinism measurements on REAL objects.

Two questions, both answered by running the real pipeline on the SILVER new
objects (extracted under `E:\\CASE10_new_objects_extracted`, manifests under
`evaluation/silver_new_objects_pass1/manifests`), each run in its own fresh
interpreter (fresh page/embedding caches, like a real re-launch):

1. `timing`   -- how long does tagging an object take, cold, at N workers?
                 (`--object DOO25 --workers 12`), with the plan/scan/merge
                 split, so the serial fraction can be read off for an Amdahl
                 extrapolation to the grading server's 24 physical cores.
2. `determinism` -- the acceptance criterion: full `run_process` twice (or
                 more) on the SAME object under different worker counts, CPU
                 load, torch thread counts and cache state, and compare the
                 SHA-256 of the resulting `submission.json` (serialised exactly
                 as `protocol_to_submission` returns it, `indent=2`, no key
                 sorting -- so key/row ORDER differences count too).

Usage (from the repo root, project venv):
    python evaluation/live_tagger_scale_check.py timing      --object LOS3A --workers 1 4 12
    python evaluation/live_tagger_scale_check.py determinism --object LOS3A
Writes JSON under evaluation/reports/live_tagger_scale/ (regenerable).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "reports" / "live_tagger_scale"
MANIFESTS = HERE / "silver_new_objects_pass1" / "manifests"
EXTRACTED_ROOT = Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"))
OBJECTS = {
    "LOS3A": "Лосевская, 3А",
    "ALT79B": "Алтуфьевское, 79Б",
    "POL17": "Полярная, 17",
    "DOO25": "Полярная ул. 25_ ДОО220к.9",
    "OKT103": "Октябрьская 103",
    "IZM12": "Изумрудная, 12",
}


# --------------------------------------------------------------------- child


def _child(args: argparse.Namespace) -> None:
    """One measured run in this (fresh) interpreter. Env was prepared by the driver
    BEFORE this process started, so `app.config.settings` picks it up."""
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "api_service"))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.domain.live_candidate_tagger import new_live_tagger_budget, tag_live_candidates
    from app.domain.official_dataset import _import_document_manifest
    from app.domain.official_evidence import _full_catalog_params
    from app.domain.v3_pipeline import (
        MATRIX_VERSION_OFFICIAL, _project_documents, create_process, current_document_versions, latest_protocol,
        list_active_params, protocol_to_dict, run_process,
    )
    from evaluation.exporter import protocol_to_submission

    manifest_path = MANIFESTS / f"manifest_{args.object}.jsonl"
    object_id = json.loads(manifest_path.open(encoding="utf-8").readline())["object_id"]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    _import_document_manifest(db, manifest_path, project_id=1, organization_id=1, object_ids={object_id})
    process = create_process(db, project_id=1, organization_id=1, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
    import app.domain.live_tagger_scan as _scan

    summary: dict = {"object": args.object, "mode": args.mode, "pid": os.getpid(), "code_root": str(REPO),
                     "scan_code_fingerprint": _scan.scan_code_fingerprint()}

    if args.mode == "tagger":
        docs = current_document_versions(_project_documents(db, process))
        params = list_active_params(
            db, organization_id=1, project_id=1, matrix_version=str(process.matrix_version or MATRIX_VERSION_OFFICIAL),
        )
        started = time.monotonic()
        diagnostics = tag_live_candidates(db, docs, _full_catalog_params(db, params), budget=new_live_tagger_budget())
        summary["wall_seconds"] = round(time.monotonic() - started, 2)
        summary["diagnostics"] = diagnostics
    else:
        started = time.monotonic()
        run_process(db, process_id=process.id)
        summary["wall_seconds"] = round(time.monotonic() - started, 2)
        protocol = latest_protocol(db, project_id=1, organization_id=1, process_id=process.id)
        submission = protocol_to_submission(protocol_to_dict(protocol))
        data = json.dumps(submission, ensure_ascii=False, indent=2).encode("utf-8")
        Path(args.out).with_suffix(".submission.json").write_bytes(data)
        summary["submission_sha256"] = hashlib.sha256(data).hexdigest()
        summary["checks"] = len(submission["checks"])
        summary["coverage_summary"] = (protocol.payload_json or {}).get("live_tagger_coverage", {}).get("summary")
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# -------------------------------------------------------------------- driver


class _Load:
    """N busy-loop processes hogging CPUs while a run is in flight."""

    def __init__(self, n: int):
        self.procs = [
            subprocess.Popen([sys.executable, "-c", "while True: pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(n)
        ]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for proc in self.procs:
            proc.kill()
        for proc in self.procs:
            proc.wait()


CODE_ROOT: Path | None = None   # set from --code-root: run children against a frozen copy of the code


def _run_child(obj: str, mode: str, label: str, *, workers: int, cache_dir: Path | None, hogs: int = 0,
               threads: int | None = None, extra_env: dict[str, str] | None = None) -> dict:
    out = OUT_DIR / f"{obj}_{mode}_{label}.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "HF_HUB_OFFLINE": "1",                       # dev box: TLS interception makes HF retry HEADs
        "CASE10_LLM_VERIFIER_ENABLED": "0",          # production default; pinned for reproducibility
        "CASE10_ORIGINALS_ROOT": str(EXTRACTED_ROOT / OBJECTS[obj]),
        "CASE10_LIVE_TAGGER_WORKERS": str(workers),
        "CASE10_LIVE_TAGGER_CACHE_ENABLED": "1" if cache_dir else "0",
        "PYTHONIOENCODING": "utf-8",
    })
    if cache_dir:
        env["CASE10_LIVE_TAGGER_CACHE_DIR"] = str(cache_dir)
    if threads:
        env.update({"OMP_NUM_THREADS": str(threads), "MKL_NUM_THREADS": str(threads)})
    env.update(extra_env or {})
    # Children run from the REPOSITORY ROOT (dataset roots are discovered from the CWD) but, with --code-root, import the
    # code from a frozen copy: other jobs edit this working tree while a multi-run determinism check is in flight, and a
    # code change between two runs would be indistinguishable from non-determinism.
    script = (CODE_ROOT / "evaluation" / "live_tagger_scale_check.py") if CODE_ROOT else Path(__file__).resolve()
    cmd = [sys.executable, str(script), "child", "--object", obj, "--mode", mode, "--out", str(out)]
    # Other jobs share this machine (parallel agents, Docker): record how busy it is BEFORE the run so a
    # wall-clock number can be read for what it is. `scan_cpu_seconds` (worker CPU time) is the load-robust figure.
    try:
        import psutil
        baseline_cpu = psutil.cpu_percent(interval=3.0)
    except ImportError:
        baseline_cpu = None
    with _Load(hogs):
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out.exists():
        raise SystemExit(f"child failed ({label}):\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}")
    result = json.loads(out.read_text(encoding="utf-8"))
    result.update({"label": label, "workers_requested": workers, "hogs": hogs, "cache": bool(cache_dir), "omp_threads": threads,
                   "baseline_cpu_percent_before_run": baseline_cpu})
    return result


def _timing(args: argparse.Namespace) -> None:
    rows = []
    for workers in args.workers:
        r = _run_child(args.object, "tagger", f"w{workers}", workers=workers, cache_dir=None)
        d = r["diagnostics"]
        row = {
            "workers": d["workers"], "wall_seconds": r["wall_seconds"], "plan_s": d["plan_seconds"], "scan_s": d["scan_seconds"],
            "merge_s": d["merge_seconds"], "documents_scanned": d["documents_scanned"], "pages_scanned": d["pages_scanned"],
            "fragments": d["fragments_created"], "failed": d["documents_failed"],
            "scan_cpu_s": d["scan_cpu_seconds"], "baseline_cpu_percent_before_run": r["baseline_cpu_percent_before_run"],
            "pages_per_second": round(d["pages_scanned"] / max(d["scan_seconds"], 1e-9), 1),
            "coverage": d["coverage"]["summary"],
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    if args.skip_cache:
        (OUT_DIR / f"{args.object}_timing_{'_'.join(map(str, args.workers))}.json").write_text(
            json.dumps({"object": args.object, "runs": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        return
    # cold vs warm cache, at the widest setting
    with tempfile.TemporaryDirectory() as tmp:
        widest = max(args.workers)
        cold = _run_child(args.object, "tagger", "cache_cold", workers=widest, cache_dir=Path(tmp))
        warm = _run_child(args.object, "tagger", "cache_warm", workers=widest, cache_dir=Path(tmp))
    cache = {
        "cold_wall_seconds": cold["wall_seconds"], "warm_wall_seconds": warm["wall_seconds"],
        "warm_cache_hits": warm["diagnostics"]["cache_hits"], "warm_workers": warm["diagnostics"]["workers"],
        "warm_scan_seconds": warm["diagnostics"]["scan_seconds"],
    }
    print(json.dumps({"cache": cache}), flush=True)
    (OUT_DIR / f"{args.object}_timing.json").write_text(
        json.dumps({"object": args.object, "runs": rows, "cache": cache}, ensure_ascii=False, indent=1), encoding="utf-8")


def _determinism(args: argparse.Namespace) -> None:
    hi = args.workers[-1] if args.workers else 8
    with tempfile.TemporaryDirectory() as cache_tmp:
        cache = Path(cache_tmp)
        plans = [
            # label, workers, cache, hogs, omp threads
            ("A_sequential_idle", 1, None, 0, None),
            (f"B_pool{hi}_idle", hi, None, 0, None),
            (f"C_pool{max(2, hi // 3)}_under_load", max(2, hi // 3), None, args.hogs, 1),
            (f"D_pool{hi}_cache_cold", hi, cache, 0, None),
            (f"E_pool{hi}_cache_warm_under_load", hi, cache, args.hogs, 2),
            ("F_sequential_cache_warm", 1, cache, 0, 1),
        ]
        if args.only:
            plans = [plan for plan in plans if plan[0][0] in args.only.upper()]
        results = []
        for label, workers, cache_dir, hogs, threads in plans:
            r = _run_child(args.object, "pipeline", label, workers=workers, cache_dir=cache_dir, hogs=hogs, threads=threads)
            results.append(r)
            print(json.dumps({k: r[k] for k in ("label", "workers_requested", "hogs", "cache", "omp_threads", "wall_seconds", "checks", "submission_sha256")}, ensure_ascii=False), flush=True)
    digests = {r["submission_sha256"] for r in results}
    verdict = {"object": args.object, "runs": len(results), "distinct_sha256": len(digests), "byte_identical": len(digests) == 1,
               "sha256": sorted(digests), "coverage": results[0].get("coverage_summary")}
    (OUT_DIR / f"{args.object}_determinism.json").write_text(
        json.dumps({"verdict": verdict, "runs": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False), flush=True)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console codepage would mangle the Russian summary
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("timing", "determinism", "child"):
        p = sub.add_parser(name)
        p.add_argument("--object", required=True, choices=sorted(OBJECTS))
        p.add_argument("--workers", type=int, nargs="*", default=[1, 4, 12] if name == "timing" else [8])
        p.add_argument("--hogs", type=int, default=6, help="busy-loop processes during the loaded runs")
        p.add_argument("--mode", default="tagger", choices=("tagger", "pipeline"))
        p.add_argument("--out", default="")
        p.add_argument("--only", default="", help="determinism: run only these configurations, e.g. ACE")
        p.add_argument("--skip-cache", action="store_true", help="timing: skip the cold/warm cache pair")
        p.add_argument("--code-root", default="", help="frozen copy of the code (api_service/app, evaluation/, shared/) to import in the runs")
    args = parser.parse_args()
    global CODE_ROOT
    if getattr(args, "code_root", ""):
        CODE_ROOT = Path(args.code_root).resolve()
    {"timing": _timing, "determinism": _determinism, "child": _child}[args.cmd](args)


if __name__ == "__main__":
    main()
