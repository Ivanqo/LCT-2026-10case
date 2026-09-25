"""LLM-verifier pilot, phase 1 (project venv, CPU): run the REAL v3 pipeline
(run_process, full 132-param official catalog, live candidate tagger) on the
6 new objects exactly like the live-candidate-tagger session did, and record
EVERY candidate pool that reaches cross_stage_localization.pick_best_candidate
-- plus the deterministic pick -- together with the per-candidate row/context
text the LLM verifier would see. No inference here; output is JSONL consumed
by phase 2 in the isolated GPU venv."""
import csv
import hashlib
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(r"D:\Proga\LCT-hack-2026")
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "api_service"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, EvidenceGroup, Param, SourceFragment
import app.domain.dataset_sources as dataset_sources
import app.domain.cross_stage_localization as csl
import app.domain.generic_matrix_extraction as gm
import app.domain.generic_enum_extraction as ge
import app.domain.generic_compound_extraction as gc
import app.domain.generic_table_row_count as gt
from app.domain.llm_candidate_verifier import build_candidate_context, parameter_definition
from app.domain.official_dataset import _import_document_manifest
from app.domain.v3_pipeline import create_process, run_process, MATRIX_VERSION_OFFICIAL

SCRATCH = Path(__file__).parent
OUT = SCRATCH / "candidate_pools.jsonl"
PREV = Path(r"C:\Users\Ivan\AppData\Local\Temp\claude\D--Proga-LCT-hack-2026\d286b25c-7531-4f52-95c5-a0f80ff8ee00\scratchpad")
EXTRACTED_ROOT = Path(r"E:\CASE10_new_objects_extracted")
OBJECTS = [
    ("LOS3A", "Лосевская, 3А"),
    ("ALT79B", "Алтуфьевское, 79Б"),
    ("POL17", "Полярная, 17"),
    ("DOO25", "Полярная ул. 25_ ДОО220к.9"),
    ("OKT103", "Октябрьская 103"),
    ("IZM12", "Изумрудная, 12"),
]

# ---- capture machinery --------------------------------------------------
_current = {"object": None, "mechanism": None, "params": {}}
_context_by_payload: dict[int, tuple] = {}
_records: list[dict] = []


def _wrap_match_fn(match_fn):
    def wrapped(snapshot, fragment, doc, page, extra):
        outcome = match_fn(snapshot, fragment, doc, page, extra)
        if outcome is not None:
            payload = outcome[0]
            row_text, context = build_candidate_context(snapshot.get("words") or [], getattr(payload, "bbox_pdf", None))
            _context_by_payload[id(payload)] = (row_text, context, outcome[2])
        return outcome
    return wrapped


def _display_value(payload):
    for attr in ("display_value", "value", "normalized_value"):
        v = getattr(payload, attr, None)
        if v is not None:
            return str(v)
    return None


_orig_pick = csl.pick_best_candidate


def _recording_pick(candidates, reference_fingerprint):
    best = _orig_pick(candidates, reference_fingerprint)
    if candidates:
        frag = candidates[0].fragment
        code = (frag.metadata_json or {}).get("code") or getattr(frag, "code", None)
        doc = candidates[0].document
        stage = doc.dataset_stage
        param = _current["params"].get(code)
        rec = {
            "object": _current["object"],
            "mechanism": _current["mechanism"],
            "code": code,
            "stage": stage,
            "has_reference_fingerprint": reference_fingerprint is not None,
            "definition": parameter_definition(param, stage) if param is not None else {"code": code, "stage": stage},
            "baseline_choice": None if best is None else next(i for i, c in enumerate(candidates) if c is best),
            "candidates": [],
        }
        for c in candidates:
            row_text, context, semantic_text = _context_by_payload.get(id(c.payload), (None, None, None))
            rec["candidates"].append({
                "document": c.document.filename,
                "document_stage": c.document.dataset_stage,
                "discipline": c.document.discipline,
                "page": c.page,
                "value": _display_value(c.payload),
                "row_text": row_text,
                "context": context,
                "semantic_text": semantic_text,
                "payload_context": getattr(c.payload, "context", None),
                "semantic_score": c.semantic_score,
                "is_empty_value": c.is_empty_value,
                "normative_mismatch": c.normative_mismatch,
                "fragment_confidence": c.fragment.confidence,
                "source_system": c.fragment.source_system,
            })
        _records.append(rec)
    return best


csl.pick_best_candidate = _recording_pick

_orig_resolve = csl.resolve_stage_round


def _make_resolve(mechanism):
    def resolve(*, entries, by_id, pages_per_stage, budget, match_fn):
        _current["mechanism"] = mechanism
        return _orig_resolve(entries=entries, by_id=by_id, pages_per_stage=pages_per_stage, budget=budget, match_fn=_wrap_match_fn(match_fn))
    return resolve


for module, name in ((gm, "numeric"), (ge, "enum"), (gc, "compound"), (gt, "row_count")):
    module.resolve_stage_round = _make_resolve(name)


# ---- manifest (same as live-candidate-tagger session, already hash-verified) ----
def patch_document_bytes(object_root: Path):
    def patched(document):
        meta = document.dataset_metadata or {}
        row = meta.get("document_manifest") or meta.get("files_index") or {}
        rel = str(row.get("relative_path") or "").replace("/", "\\")
        return (object_root / rel).read_bytes()
    dataset_sources.original_document_bytes = patched


def run_object(code, dirname):
    object_root = EXTRACTED_ROOT / dirname
    manifest_path = PREV / ("los3a_manifest.jsonl" if code == "LOS3A" and not (PREV / "manifest_LOS3A.jsonl").exists() else f"manifest_{code}.jsonl")
    object_id = f"OBJ-NEW-{code}"
    # los3a_manifest.jsonl may carry a different object_id -- read it from the file
    with open(manifest_path, encoding="utf-8") as f:
        first = json.loads(f.readline())
    object_id = first["object_id"]
    patch_document_bytes(object_root)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    _import_document_manifest(db, manifest_path, project_id=1, organization_id=1, object_ids={object_id})
    process = create_process(db, project_id=1, organization_id=1, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
    _current["object"] = code
    _current["params"] = {p.code: p for p in db.query(Param).all()}
    before = len(_records)
    t0 = time.monotonic()
    run_process(db, process_id=process.id)
    dt = time.monotonic() - t0
    groups = db.query(EvidenceGroup).filter(EvidenceGroup.object_id == object_id).all()
    comparable = sorted({g.param.code for g in groups if g.comparability_status == "COMPARABLE"})
    live = db.query(SourceFragment).filter(SourceFragment.source_system == "live_tagger").count()
    summary = {"object": code, "run_seconds": round(dt, 1), "live_fragments": live, "comparable_codes": comparable,
               "pools_captured": len(_records) - before}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    db.close()
    return summary


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    summaries = []
    for code, dirname in OBJECTS:
        if only and code not in only:
            continue
        summaries.append(run_object(code, dirname))
        with open(OUT, "w", encoding="utf-8") as f:
            for r in _records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with open(SCRATCH / "capture_summary.json", "w", encoding="utf-8") as f:
            json.dump(summaries, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
