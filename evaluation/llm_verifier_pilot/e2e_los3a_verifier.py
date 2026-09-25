"""End-to-end: the REAL v3 pipeline (run_process, full official catalog, live
tagger) on LOS3A with CASE10_LLM_VERIFIER_ENABLED=1 -- the verifier runs
inside cross_stage_localization.resolve_stage_round exactly as in production
(no harness-side prompt code). Dumps every generic-tier evidence group's
per-stage values and delta.llm_verification. Run in the GPU venv."""
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(r"D:\Proga\LCT-hack-2026")
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "api_service"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, EvidenceGroup
import app.domain.dataset_sources as dataset_sources
from app.domain.official_dataset import _import_document_manifest
from app.domain.v3_pipeline import create_process, run_process, MATRIX_VERSION_OFFICIAL

SCRATCH = Path(__file__).parent
MANIFEST = Path(r"C:\Users\Ivan\AppData\Local\Temp\claude\D--Proga-LCT-hack-2026\d286b25c-7531-4f52-95c5-a0f80ff8ee00\scratchpad\los3a_manifest.jsonl")
ROOT = Path(r"E:\CASE10_new_objects_extracted\Лосевская, 3А")
OUT = SCRATCH / f"e2e_los3a_{os.environ.get('CASE10_LLM_VERIFIER_MODE', 'shadow')}_{'on' if os.environ.get('CASE10_LLM_VERIFIER_ENABLED') == '1' else 'off'}.json"


def patched(document):
    meta = document.dataset_metadata or {}
    row = meta.get("document_manifest") or meta.get("files_index") or {}
    return (ROOT / str(row.get("relative_path") or "").replace("/", "\\")).read_bytes()


dataset_sources.original_document_bytes = patched
engine = create_engine("sqlite+pysqlite:///:memory:")
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
_import_document_manifest(db, MANIFEST, project_id=1, organization_id=1, object_ids={"OBJ-NEW-LOS3A"})
process = create_process(db, project_id=1, organization_id=1, object_id="OBJ-NEW-LOS3A", matrix_version=MATRIX_VERSION_OFFICIAL)
t0 = time.monotonic()
run_process(db, process_id=process.id)
dt = time.monotonic() - t0
groups = db.query(EvidenceGroup).filter(EvidenceGroup.object_id == "OBJ-NEW-LOS3A").all()
rows = []
for g in groups:
    d = g.delta or {}
    if d.get("source") not in ("generic_anchor_extractor",) and "llm_verification" not in d and g.comparability_status != "COMPARABLE":
        continue
    rows.append({
        "code": g.param.code, "comparability": g.comparability_status, "finding": g.finding_status,
        "expected": g.expected_value, "actual": g.actual_value, "values": d.get("values"),
        "llm_verification": d.get("llm_verification"),
    })
rows.sort(key=lambda r: r["code"])
json.dump({"run_seconds": round(dt, 1), "comparable": sorted(r["code"] for r in rows if r["comparability"] == "COMPARABLE"), "groups": rows},
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("done", OUT, round(dt, 1))
