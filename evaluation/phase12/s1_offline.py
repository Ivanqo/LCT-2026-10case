"""S1 offline runner: the explication mechanism (table_parser + explication_compare) on one object WITHOUT the DB
pipeline -- same document selection (document_facts tags + gate-equivalent integrity/revision analysis), same scan,
pairing and comparison as `collect_explication_groups`. For development and for the S1 report's per-object tables.

    python evaluation/phase12/s1_offline.py --object ALT79B [--out file.json]
    python evaluation/phase12/s1_offline.py --kit            (the organizer's markup-kit page pairs)

Objects are read from CASE10_NEW_OBJECTS_ROOT (default E:/CASE10_new_objects_extracted) via our SILVER manifests.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "api_service"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.domain import document_facts as df  # noqa: E402
from app.domain import explication_compare as ec  # noqa: E402
from app.domain import table_parser as tpar  # noqa: E402

NEW_OBJECTS_ROOT = Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"))
MANIFESTS = REPO_ROOT / "evaluation" / "silver_new_objects_pass1" / "manifests"
FOLDERS = {"LOS3A": "Лосевская, 3А", "ALT79B": "Алтуфьевское, 79Б", "POL17": "Полярная, 17",
           "DOO25": "Полярная ул. 25_ ДОО220к.9", "OKT103": "Октябрьская 103", "IZM12": "Изумрудная, 12"}
KIT_PDF = REPO_ROOT / "case_data" / "official_1_1_20260925" / "Комплект_предметной_разметки.pdf"
# the markup kit's page pairs (organizer data used for DEVELOPMENT CHECKS only, never by the mechanism)
KIT_PAIRS = {"ALT79B": ((2, 3), (4, 5)), "POL16": ((15, 16),), "DOO25": ((17, 18),), "SOSH25": ((19, 20),), "POL17-N01": ((21, 22), (23, 24))}


class _Gate:
    """The part of comparison_gate.GateContext the mechanism reads, built from manifest rows (no DB)."""

    def __init__(self, facts: dict[int, Any]):
        self.facts = facts
        integrity = df.analyze_integrity(facts.values())
        self.revisions = df.analyze_revisions(facts.values(), exclude_keys=integrity.excluded_keys)
        key_to_id = {f.key: i for i, f in facts.items()}
        self.excluded = {key_to_id[k] for k in integrity.excluded_keys if k in key_to_id}
        self.superseded = {key_to_id[k] for k in self.revisions.superseded if k in key_to_id}

    def drop_from_generic_candidates(self, doc_id: int) -> bool:
        return int(doc_id) in self.excluded or int(doc_id) in self.superseded


def object_docs(obj: str) -> tuple[list[Any], _Gate]:
    rows = [json.loads(line) for line in (MANIFESTS / f"manifest_{obj}.jsonl").open(encoding="utf-8")]
    docs, facts = [], {}
    for index, row in enumerate(rows, start=1):
        facts[index] = df.facts_from_manifest_row(row, doc_id=index)
        docs.append(SimpleNamespace(id=index, dataset_file_id=row["file_id"], dataset_stage=row["stage"], doc_stage=None,
                                    dataset_metadata={"document_manifest": {"relative_path": row["relative_path"], "pdf_pages": row.get("pdf_pages")}},
                                    file_hash=row["sha256"], content_hash=row["sha256"], discipline=row.get("section"),
                                    document_code=None, revision=None, approval_status=None))
    return docs, _Gate(facts)


def discrepancy_row(result: ec.PairResult, d: ec.Discrepancy) -> dict[str, Any]:
    return {"type": d.type, "code": d.code, "location": d.location, "field": d.field, "expected": d.expected, "actual": d.actual,
            "violation": d.violation, "details": d.details,
            "pd": {"file_id": result.pd.file_id, "page": result.pd.page, "row_bbox_norm": result.pd.table.bbox_norm(d.pd_row.bbox) if d.pd_row else None},
            "rd": {"file_id": result.rd.file_id, "page": result.rd.page, "row_bbox_norm": result.rd.table.bbox_norm(d.rd_row.bbox) if d.rd_row else None}}


def pair_row(result: ec.PairResult) -> dict[str, Any]:
    return {"pd": {"file_id": result.pd.file_id, "page": result.pd.page, "title": result.pd.table.title, "scope": result.pd.table.scope.to_dict(),
                   "rows": len(result.pd.table.rows), "bbox_norm": result.pd.table.bbox_norm()},
            "rd": {"file_id": result.rd.file_id, "page": result.rd.page, "title": result.rd.table.title, "scope": result.rd.table.scope.to_dict(),
                   "rows": len(result.rd.table.rows), "bbox_norm": result.rd.table.bbox_norm()},
            "pd_copies": [c.page for c in result.pd_copies], "rd_copies": [c.page for c in result.rd_copies],
            "score": result.score, "compared_rows": result.compared_rows, "alternatives": [a.file_id for a in result.alternatives],
            "suppressed": result.suppressed, "notes": result.notes,
            "discrepancies": [discrepancy_row(result, d) for d in result.discrepancies]}


def run_object(obj: str) -> dict[str, Any]:
    os.environ["CASE10_ORIGINALS_ROOT"] = str(NEW_OBJECTS_ROOT / FOLDERS[obj])
    docs, gate = object_docs(obj)
    started = time.perf_counter()
    candidates = ec.candidate_documents(docs, gate)
    refs, diagnostics = ec.build_table_refs(candidates, gate)
    scan_seconds = time.perf_counter() - started
    results = ec.compare_pairs(refs["PD"], refs["RD"], conflicting=ec._conflicts(gate, candidates), policy=ec.revision_policy())
    return {"object": obj, "seconds_scan": round(scan_seconds, 2), "seconds_total": round(time.perf_counter() - started, 2),
            "diagnostics": diagnostics, "candidate_files": [d.dataset_file_id for d in candidates],
            "tables": {stage: [{"file_id": r.file_id, "page": r.page, "kind": r.table.kind, "title": r.table.title, "scope": r.table.scope.to_dict(), "rows": len(r.table.rows)} for r in refs[stage]] for stage in refs},
            "pairs": [pair_row(r) for r in results]}


def kit_snapshot(page: int) -> dict[str, Any]:
    import fitz

    from app.domain.anchor_search import cluster_rows
    from app.domain.dataset_sources import _bbox_to_visible_frame, _visible_page_dims

    with fitz.open(KIT_PDF) as pdf:
        p = pdf[page - 1]
        rotation = int(p.rotation)
        rw, rh = float(p.cropbox.width), float(p.cropbox.height)
        width, height = _visible_page_dims(rw, rh, rotation)
        words = [{"bbox": _bbox_to_visible_frame([float(v) for v in w[:4]], rw, rh, rotation), "text": w[4]}
                 for w in p.get_text("words", sort=True) if str(w[4]).strip()]
        if rotation % 360:
            words = [w for row in cluster_rows(words) for w in row]
    return {"page": page, "width": width, "height": height, "words": words}


def run_kit() -> dict[str, Any]:
    out = {}
    for case, pairs in KIT_PAIRS.items():
        pd_refs, rd_refs = [], []
        for pd_page, rd_page in pairs:
            for stage, page, bucket in (("PD", pd_page, pd_refs), ("RD", rd_page, rd_refs)):
                for table in tpar.find_room_tables(kit_snapshot(page), page=page):
                    bucket.append(ec.TableRef(table=table, stage=stage, file_id=f"KIT-{stage}", doc_key=stage))
        results = ec.compare_pairs(pd_refs, rd_refs)
        out[case] = {"tables": {"PD": len(pd_refs), "RD": len(rd_refs)}, "pairs": [pair_row(r) for r in results]}
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object", action="append", default=[])
    parser.add_argument("--kit", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = {"kit": run_kit()} if args.kit else {}
    for obj in args.object:
        report[obj] = run_object(obj)
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
