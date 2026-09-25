"""Phase 12 / P0: run the REAL pipeline (`run_process`) per object and export the 1.1 submission, for grader_sim.

One fresh interpreter per object (environment per object, no state leaking between objects), in-process sqlite,
LLM verifier OFF, no gold imported. Modes:
  organizer  official objects with the organizer's annotations/index labels (set A)
  live       official objects with CASE10_DISABLE_ORGANIZER_ANNOTATIONS=1 (set B)
  new        the six SILVER objects from our own manifests (no organizer annotations exist for them)

Usage (repo root as CWD, so dataset roots are discovered):
    python evaluation/phase12/run_baseline.py --mode organizer --out <dir> [--code-root <frozen tree>] [OBJ ...]
Objects: TYU NOV RECH (organizer/live), LOS3A ALT79B POL17 DOO25 OKT103 IZM12 (new).
Per object: <OBJ>.submission.json (the 1.1 export), <OBJ>.run.json (timings, statuses, code fingerprint).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_WORK = Path(os.environ.get("CASE10_BENCH_WORK", r"E:\case10_measurement"))
NEW_OBJECTS_ROOT = Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"))
MANIFESTS = REPO_ROOT / "evaluation" / "silver_new_objects_pass1" / "manifests"

OFFICIAL = {"TYU": "OBJ-TYUMENSKAYA-5-GOLD-SEED", "NOV": "OBJ-NOVOSLOBODSKAYA", "RECH": "OBJ-RECHNIKOV-7-7"}
NEW = {"LOS3A": "Лосевская, 3А", "ALT79B": "Алтуфьевское, 79Б", "POL17": "Полярная, 17",
       "DOO25": "Полярная ул. 25_ ДОО220к.9", "OKT103": "Октябрьская 103", "IZM12": "Изумрудная, 12"}


def child(obj: str, mode: str, out: Path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.domain.official_dataset import HIDDEN_OBJECT_IDS, _import_document_manifest, import_official_dataset
    from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, create_process, latest_protocol, protocol_to_dict, run_process
    import app.domain.live_tagger_scan as scan
    from evaluation.exporter import protocol_to_submission

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    t0 = time.monotonic()
    if obj in OFFICIAL:
        object_id = OFFICIAL[obj]
        import_summary = import_official_dataset(db, project_id=1, organization_id=1, object_ids=[object_id], include_gold=False,
                                                 include_hidden=object_id in HIDDEN_OBJECT_IDS, allow_hidden_gold_labels=False)
        import_summary = {k: import_summary.get(k) for k in ("organizer_annotations", "files_index", "page_index", "annotations", "document_manifest")}
    else:
        manifest = MANIFESTS / f"manifest_{obj}.jsonl"
        object_id = json.loads(manifest.open(encoding="utf-8").readline())["object_id"]
        import_summary = {"document_manifest": _import_document_manifest(db, manifest, project_id=1, organization_id=1, object_ids={object_id})}
    import_seconds = time.monotonic() - t0
    process = create_process(db, project_id=1, organization_id=1, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
    t1 = time.monotonic()
    run_process(db, process_id=process.id)
    run_seconds = time.monotonic() - t1
    protocol = protocol_to_dict(latest_protocol(db, project_id=1, organization_id=1, process_id=process.id))
    submission = protocol_to_submission(protocol)
    data = json.dumps(submission, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    (out / f"{obj}.submission.json").write_bytes(data)
    findings = protocol["payload"]["findings"]
    summary = {
        "object": obj, "object_id": object_id, "mode": mode,
        "import_seconds": round(import_seconds, 1), "run_process_seconds": round(run_seconds, 1),
        "import": import_summary,
        "finding_status": dict(Counter(g.get("finding_status") for g in findings)),
        "violation_label": dict(Counter(c["violation_label"] for c in submission["checks"])),
        "checks": len(submission["checks"]),
        "submission_sha256": hashlib.sha256(data).hexdigest(),
        "scan_code_fingerprint": scan.scan_code_fingerprint(),
        "live_tagger_coverage": (protocol["payload"].get("live_tagger_coverage") or {}).get("summary"),
        "env": {key: os.environ.get(key) for key in ("CASE10_DISABLE_ORGANIZER_ANNOTATIONS", "CASE10_LIVE_TAGGER_WORKERS",
                                                      "CASE10_PARAMETER_CODE_STYLE", "CASE10_LLM_VERIFIER_ENABLED")},
    }
    (out / f"{obj}.run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("organizer", "live", "new"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--workers", default="3")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("objects", nargs="*")
    args = parser.parse_args()
    code_root = args.code_root.resolve()
    if args.child:
        sys.path[:0] = [str(code_root), str(code_root / "api_service")]
        child(args.child, args.mode, args.out)
        return
    objects = args.objects or (list(NEW) if args.mode == "new" else list(OFFICIAL))
    args.out.mkdir(parents=True, exist_ok=True)
    for obj in objects:
        env = dict(os.environ)
        env.update({
            "HF_HUB_OFFLINE": "1", "CASE10_LLM_VERIFIER_ENABLED": "0", "PYTHONIOENCODING": "utf-8",
            "CASE10_LIVE_TAGGER_WORKERS": str(args.workers),
            "CASE10_LIVE_TAGGER_CACHE_DIR": env.get("CASE10_LIVE_TAGGER_CACHE_DIR", str(BENCH_WORK / "tagger_cache")),
            "CASE10_DISABLE_ORGANIZER_ANNOTATIONS": "1" if args.mode == "live" else "0",
        })
        env.pop("CASE10_ORIGINALS_ROOT", None)
        if obj in NEW:
            env["CASE10_ORIGINALS_ROOT"] = str(NEW_OBJECTS_ROOT / NEW[obj])
        elif (BENCH_WORK / "public_docs" / obj).is_dir():
            env["CASE10_ORIGINALS_ROOT"] = str(BENCH_WORK / "public_docs" / obj)
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--mode", args.mode, "--out", str(args.out),
                               "--code-root", str(code_root), "--child", obj], env=env, cwd=str(REPO_ROOT),
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        (args.out / f"{obj}.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr[-20000:], encoding="utf-8")
        print(json.dumps({"object": obj, "mode": args.mode, "rc": proc.returncode, "wall_seconds": round(time.monotonic() - started, 1)}), flush=True)


if __name__ == "__main__":
    main()
