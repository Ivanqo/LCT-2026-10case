"""Phase 10 / prompt B: run the REAL v3 pipeline on the 6 SILVER new objects and dump every EvidenceGroup.

Same run as `evaluation/silver_new_objects_pass1/snapshot_pipeline.py` (real `run_process`, official 132-param
catalog, live candidate tagger, in-process sqlite, LLM verifier OFF) with two differences that make a
before/after comparison possible without touching the frozen pass-1 reference snapshots:
  * `--code-root` imports the pipeline from another tree (e.g. a frozen copy of the code BEFORE prompt B);
  * `--out` is mandatory, so this script can never overwrite `mechanism_snapshots/`.
The tagger result cache (`data/live_tagger_cache`, keyed by file SHA-256 + scan-code fingerprint) is shared, so the
second tree is fast.

Usage (repo root as CWD):
    python evaluation/decision_layer_b/run_silver_objects.py --out <dir> [--code-root <tree>] [OBJ ...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MANIFESTS = REPO_ROOT / "evaluation" / "silver_new_objects_pass1" / "manifests"
EXTRACTED_ROOT = Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"))
OBJECTS = [
    ("LOS3A", "Лосевская, 3А"),
    ("ALT79B", "Алтуфьевское, 79Б"),
    ("POL17", "Полярная, 17"),
    ("DOO25", "Полярная ул. 25_ ДОО220к.9"),
    ("OKT103", "Октябрьская 103"),
    ("IZM12", "Изумрудная, 12"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--code-root", type=Path, default=REPO_ROOT)
    parser.add_argument("objects", nargs="*")
    args = parser.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ["CASE10_LLM_VERIFIER_ENABLED"] = "0"
    code_root = args.code_root.resolve()
    sys.path.insert(0, str(code_root))
    sys.path.insert(0, str(code_root / "api_service"))

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base, EvidenceFragment, EvidenceGroup, SourceFragment
    import app.domain.dataset_sources as dataset_sources
    from app.domain.official_dataset import _import_document_manifest
    from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, build_protocol_payload, create_process, run_process

    args.out.mkdir(parents=True, exist_ok=True)
    for code, dirname in OBJECTS:
        if args.objects and code not in args.objects:
            continue
        object_root = EXTRACTED_ROOT / dirname
        manifest_path = MANIFESTS / f"manifest_{code}.jsonl"
        object_id = json.loads(manifest_path.open(encoding="utf-8").readline())["object_id"]

        def patched(document, _root=object_root):
            meta = document.dataset_metadata or {}
            row = meta.get("document_manifest") or meta.get("files_index") or {}
            rel = str(row.get("relative_path") or "").replace("/", "\\")
            return (_root / rel).read_bytes()

        dataset_sources.original_document_bytes = patched
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        _import_document_manifest(db, manifest_path, project_id=1, organization_id=1, object_ids={object_id})
        process = create_process(db, project_id=1, organization_id=1, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
        started = time.monotonic()
        run_process(db, process_id=process.id)
        seconds = round(time.monotonic() - started, 1)
        groups = []
        for group in db.query(EvidenceGroup).filter(EvidenceGroup.object_id == object_id).all():
            fragments = db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == group.id).all()
            groups.append({
                "parameter_code": group.param.code if group.param else None,
                "group_key": group.group_key,
                "finding_status": group.finding_status,
                "comparability_status": group.comparability_status,
                "model_version": group.model_version,
                "expected_value": group.expected_value,
                "actual_value": group.actual_value,
                "delta": group.delta,
                "fragments": [
                    {"stage": f.stage, "role": f.role, "dataset_file_id": f.dataset_file_id, "page": f.page,
                     "extracted_value": f.extracted_value, "context": f.context, "extractor": f.extractor}
                    for f in fragments
                ],
            })
        payload = build_protocol_payload(db, process)
        result = {
            "object_code": code, "object_id": object_id, "run_seconds": seconds,
            "live_fragments": db.query(SourceFragment).filter(SourceFragment.source_system == "live_tagger").count(),
            "document_analysis": payload.get("document_analysis"),
            "groups": groups,
        }
        (args.out / f"snapshot_{code}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(json.dumps({"object": code, "seconds": seconds, "groups": len(groups),
                          "statuses": dict(Counter(g["finding_status"] for g in groups)),
                          "reasons": dict(Counter((g["delta"] or {}).get("reason") for g in groups if (g["delta"] or {}).get("reason")))},
                         ensure_ascii=False), flush=True)
        db.close()


if __name__ == "__main__":
    main()
