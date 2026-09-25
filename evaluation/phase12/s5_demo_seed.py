"""Phase 12 / S5: build a dedicated demo database for the inspector workbench (browser walkthrough, DEMO stream).

Runs the REAL pipeline (`import_official_dataset` + `run_process`) for one official object into its own SQLite file,
so the shared dev DB (`data/api.db`) and its admin credentials are never touched. No gold is imported
(`include_gold=False`); organizer annotations are on by default (set A), `--live` switches them off (set B).

Usage (repo root as CWD):
    python evaluation/phase12/s5_demo_seed.py --data-dir data/s5_demo [--object TYU] [--live] [--workers 3]
Then start the API against the same data dir (see evaluation/phase12/S5_REPORT.md, "Как поднять демо").
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_WORK = Path(os.environ.get("CASE10_BENCH_WORK", r"E:\case10_measurement"))
NEW_OBJECTS_ROOT = Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"))
MANIFESTS = REPO_ROOT / "evaluation" / "silver_new_objects_pass1" / "manifests"
OFFICIAL = {"TYU": "OBJ-TYUMENSKAYA-5-GOLD-SEED", "NOV": "OBJ-NOVOSLOBODSKAYA"}
NEW = {"ALT79B": "Алтуфьевское, 79Б", "POL17": "Полярная, 17", "LOS3A": "Лосевская, 3А"}
PROJECT_NAMES = {"TYU": "Демо: Тюменская, 5", "NOV": "Демо: Новослободская", "ALT79B": "Демо: Алтуфьевское, 79Б",
                 "POL17": "Демо: Полярная, 17", "LOS3A": "Демо: Лосевская, 3А"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--object", choices=sorted({**OFFICIAL, **NEW}), default="TYU")
    parser.add_argument("--live", action="store_true", help="CASE10_DISABLE_ORGANIZER_ANNOTATIONS=1 (set B)")
    parser.add_argument("--workers", default="3")
    parser.add_argument("--admin-password", default=os.environ.get("DEFAULT_ADMIN_PASSWORD", "S5demo!2026"))
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "API_DATA_DIR": str(data_dir),
        "DEFAULT_ADMIN_LOGIN": os.environ.get("DEFAULT_ADMIN_LOGIN", "admin"),
        "DEFAULT_ADMIN_PASSWORD": args.admin_password,
        "HF_HUB_OFFLINE": "1",
        "CASE10_LLM_VERIFIER_ENABLED": "0",
        "CASE10_LIVE_TAGGER_WORKERS": str(args.workers),
        "CASE10_LIVE_TAGGER_CACHE_DIR": os.environ.get("CASE10_LIVE_TAGGER_CACHE_DIR", str(BENCH_WORK / "tagger_cache")),
        "CASE10_DISABLE_ORGANIZER_ANNOTATIONS": "1" if args.live else "0",
    })
    originals = (NEW_OBJECTS_ROOT / NEW[args.object]) if args.object in NEW else (BENCH_WORK / "public_docs" / args.object)
    if originals.is_dir():
        os.environ["CASE10_ORIGINALS_ROOT"] = str(originals)
    sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "api_service")]

    from app.db.models import Organization, Project, User
    from app.db.session import SessionLocal, init_db
    from app.domain.official_dataset import _import_document_manifest, import_official_dataset
    from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, create_process, run_process

    init_db()
    db = SessionLocal()
    admin = db.query(User).filter(User.login == os.environ["DEFAULT_ADMIN_LOGIN"]).one()
    org = db.get(Organization, int(admin.organization_id))
    name = PROJECT_NAMES[args.object] + (" (живой режим)" if args.live else "")
    project = db.query(Project).filter(Project.organization_id == org.id, Project.name == name).first()
    if project is None:
        project = Project(name=name, description="S5 inspector workbench demo", organization_id=org.id)
        db.add(project)
        db.commit()
        db.refresh(project)
    t0 = time.monotonic()
    if args.object in OFFICIAL:
        object_id = OFFICIAL[args.object]
        summary = import_official_dataset(db, project_id=project.id, organization_id=org.id, object_ids=[object_id], include_gold=False)
    else:  # SILVER new objects: our own manifest, no organizer annotations exist for them
        manifest = MANIFESTS / f"manifest_{args.object}.jsonl"
        object_id = json.loads(manifest.open(encoding="utf-8").readline())["object_id"]
        summary = {"document_manifest": _import_document_manifest(db, manifest, project_id=project.id, organization_id=org.id,
                                                                  object_ids={object_id})}
        db.commit()
    import_seconds = time.monotonic() - t0
    process = create_process(db, project_id=project.id, organization_id=org.id, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
    db.commit()
    t1 = time.monotonic()
    run_process(db, process_id=process.id, user_id=int(admin.id))
    out = {
        "data_dir": str(data_dir), "project_id": project.id, "project": name, "process_id": process.id,
        "object_id": object_id, "import_seconds": round(import_seconds, 1), "run_seconds": round(time.monotonic() - t1, 1),
        "documents": summary.get("document_manifest") or summary.get("files_index"),
        "originals_root": os.environ.get("CASE10_ORIGINALS_ROOT"),
    }
    (data_dir / f"seed_{args.object}{'_live' if args.live else ''}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, default=str))
    db.close()


if __name__ == "__main__":
    main()
