"""Step B2 harness: run the real pipeline for one object and keep the result database on disk.

Same environment as `run_baseline.py --mode live` (one fresh interpreter per object, no gold imported, LLM verifier
off), but the SQLite database is a file, so the free-hypothesis detectors can be developed and measured on it without
repeating the run (`final_b2_measure.py`). Pass the code tree with `--code-root` (a frozen `git archive HEAD` gives the
baseline, the working tree gives the result with the module).

    python evaluation/phase12/final_b2_run.py --out E:\\case10_phase12\\final_b2\\base --code-root <tree> TYU NOV
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_baseline as rb  # noqa: E402


def child(obj: str, out: Path, matrix: bool) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.domain.official_dataset import _import_document_manifest, import_official_dataset
    from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, create_process, run_process

    db_path = out / f"{obj}.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    t0 = time.monotonic()
    if obj in rb.OFFICIAL:
        object_id = rb.OFFICIAL[obj]
        import_official_dataset(db, project_id=1, organization_id=1, object_ids=[object_id])
    else:
        manifest = rb.MANIFESTS / f"manifest_{obj}.jsonl"
        object_id = json.loads(manifest.open(encoding="utf-8").readline())["object_id"]
        _import_document_manifest(db, manifest, project_id=1, organization_id=1, object_ids={object_id})
    import_seconds = time.monotonic() - t0
    process = create_process(db, project_id=1, organization_id=1, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
    t1 = time.monotonic()
    if matrix:
        run_process(db, process_id=process.id)
    run_seconds = time.monotonic() - t1
    db.commit()
    (out / f"{obj}.meta.json").write_text(json.dumps({
        "object": obj, "object_id": object_id, "process_id": process.id,
        "import_seconds": round(import_seconds, 1), "run_process_seconds": round(run_seconds, 1),
        "matrix_run": matrix, "env": {k: os.environ.get(k) for k in ("CASE10_FREE_HYPOTHESES", "CASE10_EXPLICATION_COMPARE")},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--workers", default="3")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--no-matrix", action="store_true", help="import + create the process only (table detectors do not need the matrix run)")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("objects", nargs="*")
    args = parser.parse_args()
    code_root = args.code_root.resolve()
    if args.child:
        sys.path[:0] = [str(code_root), str(code_root / "api_service")]
        child(args.child, args.out, not args.no_matrix)
        return
    args.out.mkdir(parents=True, exist_ok=True)
    for obj in args.objects:
        env = dict(os.environ)
        env.update({
            "HF_HUB_OFFLINE": "1", "CASE10_LLM_VERIFIER_ENABLED": "0", "PYTHONIOENCODING": "utf-8",
            "CASE10_LIVE_TAGGER_WORKERS": str(args.workers),
            "CASE10_LIVE_TAGGER_CACHE_DIR": env.get("CASE10_LIVE_TAGGER_CACHE_DIR", str(rb.BENCH_WORK / "tagger_cache")),
        })
        env.pop("CASE10_ORIGINALS_ROOT", None)
        if obj in rb.NEW:
            env["CASE10_ORIGINALS_ROOT"] = str(rb.NEW_OBJECTS_ROOT / rb.NEW[obj])
        elif (rb.BENCH_WORK / "public_docs" / obj).is_dir():
            env["CASE10_ORIGINALS_ROOT"] = str(rb.BENCH_WORK / "public_docs" / obj)
        started = time.monotonic()
        try:
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--out", str(args.out), "--code-root", str(code_root),
                                   "--child", obj, *(["--no-matrix"] if args.no_matrix else [])], env=env, cwd=str(REPO_ROOT), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=args.timeout)
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, stdout, stderr = -9, str(exc.stdout or ""), "TIMEOUT"
        (args.out / f"{obj}.log").write_text(stdout + "\n--- stderr ---\n" + stderr[-20000:], encoding="utf-8")
        print(json.dumps({"object": obj, "rc": rc, "wall_seconds": round(time.monotonic() - started, 1)}), flush=True)


if __name__ == "__main__":
    main()
