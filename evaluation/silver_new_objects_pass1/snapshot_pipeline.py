"""SILVER review of the 6 new objects, pass 1, step 1: run the REAL v3 pipeline
(`run_process`, full official 132-param catalog, live candidate tagger,
current code -- i.e. AFTER the LOS3A forensic fixes and with the optional LLM
verifier left at its production default, OFF) on the 6 new SILVER objects and
dump EVERY EvidenceGroup (status + values + evidence fragments) to JSON.

Deliberately writes to disk and prints only aggregate counts, never per-group
values: the manual reviewer (see silver_labels/new_objects_review_pass1.jsonl)
labels the (object, parameter) checks from the raw PDFs BEFORE opening these
snapshots, so the labels are blind to what the mechanism answered.

Usage:  python snapshot_pipeline.py [OBJ ...]      (default: all six)
Env:    HF_HUB_OFFLINE=1 is forced (Kaspersky TLS interception on the dev
        machine makes sentence-transformers retry HEADs otherwise).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ["CASE10_LLM_VERIFIER_ENABLED"] = "0"  # production default; pinned so the snapshot is reproducible

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "api_service"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.models import Base, DocumentVersion, EvidenceFragment, EvidenceGroup, Param, SourceFragment
import app.domain.dataset_sources as dataset_sources
from app.domain.live_candidate_tagger import _already_scanned
from app.domain.official_dataset import _import_document_manifest
from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, create_process, run_process

HERE = Path(__file__).resolve().parent
MANIFESTS = HERE / "manifests"
OUT_DIR = HERE / "mechanism_snapshots"
EXTRACTED_ROOT = Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"))
OBJECTS = [
    ("LOS3A", "Лосевская, 3А"),
    ("ALT79B", "Алтуфьевское, 79Б"),
    ("POL17", "Полярная, 17"),
    ("DOO25", "Полярная ул. 25_ ДОО220к.9"),
    ("OKT103", "Октябрьская 103"),
    ("IZM12", "Изумрудная, 12"),
]
PROVENANCE_MODULES = [
    "anchor_search.py", "cross_stage_localization.py", "generic_matrix_extraction.py", "generic_enum_extraction.py",
    "generic_compound_extraction.py", "generic_table_row_count.py", "live_candidate_tagger.py", "official_evidence.py",
    "official_rule_packs.py", "semantic_similarity.py", "llm_candidate_verifier.py", "dataset_sources.py",
]


def code_fingerprint() -> dict[str, str]:
    base = REPO_ROOT / "api_service" / "app" / "domain"
    return {name: hashlib.sha256((base / name).read_bytes()).hexdigest()[:16] for name in PROVENANCE_MODULES if (base / name).exists()}


def patch_document_bytes(object_root: Path) -> None:
    """Point the pipeline's original-document reader at the locally extracted
    object. Done through `CASE10_ORIGINALS_ROOT` (read from the environment at
    call time, SHA-256 verified) rather than by monkeypatching
    `original_document_bytes`: an in-process patch is invisible to the live
    tagger's worker processes and disables its on-disk cache, so the run would
    silently fall back to single-process scanning."""
    os.environ["CASE10_ORIGINALS_ROOT"] = str(object_root)


def run_object(code: str, dirname: str) -> dict:
    object_root = EXTRACTED_ROOT / dirname
    manifest_path = MANIFESTS / f"manifest_{code}.jsonl"
    object_id = json.loads(manifest_path.open(encoding="utf-8").readline())["object_id"]
    patch_document_bytes(object_root)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    _import_document_manifest(db, manifest_path, project_id=1, organization_id=1, object_ids={object_id})
    process = create_process(db, project_id=1, organization_id=1, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
    t0 = time.monotonic()
    run_process(db, process_id=process.id)
    seconds = round(time.monotonic() - t0, 1)

    groups = []
    for group in db.query(EvidenceGroup).filter(EvidenceGroup.object_id == object_id).all():
        fragments = db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == group.id).all()
        groups.append({
            "parameter_code": group.param.code if group.param else None,
            "group_key": group.group_key,
            "finding_status": group.finding_status,
            "comparability_status": group.comparability_status,
            "completeness_status": group.completeness_status,
            "model_version": group.model_version,
            "expected_value": group.expected_value,
            "actual_value": group.actual_value,
            "confidence": group.confidence,
            "delta": group.delta,
            "fragments": [
                {
                    "stage": f.stage,
                    "role": f.role,
                    "dataset_file_id": f.dataset_file_id,
                    "filename": f.document_version.filename if f.document_version else None,
                    "discipline": f.discipline,
                    "page": f.page,
                    "bbox": f.bbox,
                    "extracted_value": f.extracted_value,
                    "context": f.context,
                    "extractor": f.extractor,
                    "confidence": f.confidence,
                }
                for f in fragments
            ],
        })
    live = db.query(SourceFragment).filter(SourceFragment.source_system == "live_tagger").count()
    # Tagger coverage (documents per stage carrying a completed scan marker). The tagger's caps are
    # deterministic documents/pages limits, so this is a pure function of the object (see
    # live_candidate_tagger.live_tagger_coverage for the full per-stage breakdown, printed in the protocol).
    coverage: dict[str, dict[str, int]] = {}
    for doc in db.query(DocumentVersion).filter(DocumentVersion.object_id == object_id).all():
        stage = doc.dataset_stage or "?"
        c = coverage.setdefault(stage, {"documents": 0, "tagger_scanned": 0})
        c["documents"] += 1
        c["tagger_scanned"] += int(_already_scanned(doc))
    result = {
        "object_code": code,
        "object_id": object_id,
        "run_seconds": seconds,
        "live_fragments": live,
        "tagger_coverage": coverage,
        "tagger_budget": {k: getattr(settings, k) for k in dir(settings) if k.startswith("LIVE_TAGGER_") and not k.startswith("_")},
        "code_fingerprint": code_fingerprint(),
        "env": {"CASE10_LLM_VERIFIER_ENABLED": os.environ.get("CASE10_LLM_VERIFIER_ENABLED")},
        "groups": groups,
    }
    db.close()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"snapshot_{code}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    comparable = sum(1 for g in groups if g["comparability_status"] == "COMPARABLE")
    print(json.dumps({"object": code, "groups": len(groups), "comparable": comparable, "live_fragments": live, "seconds": seconds, "tagger_coverage": coverage}), flush=True)
    return result


def main() -> None:
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    for code, dirname in OBJECTS:
        if only and code not in only:
            continue
        run_object(code, dirname)


if __name__ == "__main__":
    main()
