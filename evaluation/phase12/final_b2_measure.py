"""Step B2: run the free-hypothesis detectors on a database written by `final_b2_run.py` (the matrix run is not repeated).

Copies the database first (the baseline stays untouched), calls `run_free_hypotheses(force=True)` with the object's
candidate documents (same selection as `official_evidence`) and writes the hypotheses with their references, plus the
per-method counts, hypotheses per 100 pages and the diagnostics.

    python evaluation/phase12/final_b2_measure.py --db E:/.../base/TYU.db --obj TYU --out E:/.../after/TYU.hyp.json [--methods A,B]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_baseline as rb  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--obj", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--methods", default="")
    parser.add_argument("--code-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args()
    code_root = args.code_root.resolve()
    sys.path[:0] = [str(code_root), str(code_root / "api_service")]
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("CASE10_LIVE_TAGGER_CACHE_DIR", str(rb.BENCH_WORK / "tagger_cache"))
    os.environ.pop("CASE10_ORIGINALS_ROOT", None)
    if args.obj in rb.NEW:
        os.environ["CASE10_ORIGINALS_ROOT"] = str(rb.NEW_OBJECTS_ROOT / rb.NEW[args.obj])
    elif (rb.BENCH_WORK / "public_docs" / args.obj).is_dir():
        os.environ["CASE10_ORIGINALS_ROOT"] = str(rb.BENCH_WORK / "public_docs" / args.obj)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import EvidenceFragment, EvidenceGroup, InspectionProcess, Param, Suspicion
    from app.domain import file_registry
    from app.domain.free_hypotheses.runner import run_free_hypotheses
    from app.domain.free_hypotheses.base import enabled_methods
    from app.domain.v3_pipeline import _project_documents, list_active_params

    work = args.out.with_suffix(".work.db")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.db, work)
    engine = create_engine(f"sqlite+pysqlite:///{work.as_posix()}")
    db = sessionmaker(bind=engine)()
    process = db.query(InspectionProcess).order_by(InspectionProcess.created_at.desc()).first()
    docs = _project_documents(db, process)
    selection = file_registry.selection_context(db, process, docs)
    candidate_docs = [d for d in docs if int(d.id) not in selection["excluded_document_ids"]]
    by_id = {d.id: d for d in candidate_docs}
    params = list_active_params(db, organization_id=process.organization_id, project_id=process.project_id, matrix_version=process.matrix_version)
    methods = [m.strip() for m in args.methods.split(",") if m.strip()] or list(enabled_methods())
    t0 = time.monotonic()
    summary = run_free_hypotheses(db, process, params, {}, by_id=by_id, methods=methods, force=True)
    db.commit()
    seconds = round(time.monotonic() - t0, 1)
    def page_count(doc) -> int:
        meta = doc.dataset_metadata or {}
        for section in ("document_manifest", "files_index"):
            block = meta.get(section) or {}
            for key in ("pdf_pages", "pages", "page_count"):
                if isinstance(block.get(key), int):
                    return int(block[key])
        return 0

    pages = sum(page_count(d) for d in candidate_docs)
    sentinel = db.query(Param).filter(Param.code == "FREE-SEARCH").first()
    rows = []
    for group in db.query(EvidenceGroup).filter(EvidenceGroup.param_id == int(sentinel.id)).order_by(EvidenceGroup.id).all() if sentinel else []:
        delta = group.delta or {}
        frags = db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == int(group.id)).order_by(EvidenceFragment.id).all()
        rows.append({
            "id": int(group.id), "method": delta.get("discovery_method"), "detector": delta.get("rule_id"), "status": group.finding_status,
            "confidence": group.confidence, "priority": group.review_priority, "location": delta.get("location"),
            "description": delta.get("description"), "pd_reference": delta.get("pd_reference"), "rd_reference": delta.get("rd_reference"),
            "expected": group.expected_value, "actual": group.actual_value, "details": delta.get("details"),
            "evidence": [{"file": f.dataset_file_id, "stage": f.stage, "page": f.page, "bbox_pdf": f.bbox_pdf, "role": f.role,
                          "value": f.extracted_value, "sha256": f.file_sha256, "doc_id": f.document_version_id} for f in frags],
        })
    by_method = Counter(r["method"] for r in rows)
    result = {
        "object": args.obj, "process_id": process.id, "documents": len(candidate_docs), "pages_in_manifest": pages,
        "seconds": seconds, "methods": methods, "count": len(rows), "by_method": dict(by_method),
        "per_100_pages": {m: round(100 * n / pages, 2) for m, n in by_method.items()} if pages else None,
        "total_per_100_pages": round(100 * len(rows) / pages, 2) if pages else None,
        "summary": summary, "hypotheses": rows,
    }
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("object", "documents", "pages_in_manifest", "seconds", "count", "by_method", "per_100_pages", "total_per_100_pages")}, ensure_ascii=False))
    db.close()
    engine.dispose()
    try:
        work.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    main()
