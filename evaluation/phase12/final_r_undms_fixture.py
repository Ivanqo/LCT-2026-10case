"""Step R validation on the organizer's UNDMS example (no fitting, no object constants in the code under test).

The original UNDMS files are not on this machine; the organizer's marking set (case_data/official_1_1_20260925/
Комплект_предметной_разметки.pdf) holds the three annotated source pages with their text layer: the PD section (stage PD),
the RD section and the RD change register. The same detectors that run on whole objects are run on those pages through the
stubbed scan layer; the annotation captions the organizer printed on the pages are in the text layer too (caveat in the report).

    python evaluation/phase12/final_r_undms_fixture.py [--pdf PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "api_service"):
    sys.path.insert(0, str(_p))

DEFAULT_PDF = REPO_ROOT / "case_data" / "official_1_1_20260925" / "Комплект_предметной_разметки.pdf"


def snapshot(pdf, page_number):
    import fitz

    page = pdf[page_number - 1]
    words = [{"text": w[4], "bbox": [float(v) for v in w[:4]]} for w in page.get_text("words", sort=True)]
    return {"words": words, "width": float(page.rect.width), "height": float(page.rect.height), "page": page_number}


def doc(doc_id, stage, name):
    return SimpleNamespace(id=doc_id, dataset_file_id=name, document_code=name, filename=name, file_hash=f"{doc_id:064x}", content_hash=f"{doc_id:064x}",
                           discipline="АР", revision="0", approval_status="UNKNOWN", dataset_stage=stage, doc_stage={"PD": "project", "RD": "working"}[stage])


def main() -> None:
    import fitz
    from app.domain.free_hypotheses import change_detect, change_sheet, layer_composition as lc, layer_detect
    from app.domain.free_hypotheses.text_utils import fold, stems

    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    args = parser.parse_args()
    pdf = fitz.open(str(args.pdf))
    pd_doc, rd_doc = doc(1, "PD", "UNDMS-PD-section"), doc(2, "RD", "UNDMS-RD-section")
    pd_snap, rd_snap, reg_snap = snapshot(pdf, 6), snapshot(pdf, 7), snapshot(pdf, 8)
    gate = SimpleNamespace(facts={1: SimpleNamespace(tags=frozenset({"АР"}), markers=(), generation=None, section_key=None),
                                  2: SimpleNamespace(tags=frozenset({"АР"}), markers=(), generation=None, section_key=None)},
                           drop_from_generic_candidates=lambda _id: False)
    result = {"pdf": str(args.pdf)}

    # LAYER_COMPOSITION: PD section vs RD section
    scans = {1: {"layers": {"compositions": lc.parse_compositions(pd_snap, page=6)}}, 2: {"layers": {"compositions": lc.parse_compositions(rd_snap, page=7)}}}
    with mock.patch("app.domain.free_hypotheses.doc_scan.scan_documents", return_value=scans):
        found, stats = layer_detect.detect([pd_doc, rd_doc], gate, object_id="UNDMS-FIXTURE")
    result["layer_composition"] = {"stats": stats, "hypotheses": [{"description": h.description, "expected": h.expected, "actual": h.actual, "location": h.location,
                                                                  "evidence": [(r.stage, r.page, r.bbox_pdf) for r in h.evidence]} for h in found]}

    # CHANGE_SHEET: RD change register vs the PD section
    entries = change_sheet.parse_change_register(reg_snap, page=8)
    # the PD look-up index is built over every page of the marking set (24 pages of nine objects) as the PD corpus, so the word
    # frequencies are those of a real drawing set rather than of one page
    corpus = {n: snapshot(pdf, n) for n in range(1, len(pdf) + 1) if n not in (7, 8)}      # the RD pages are not PD
    df: dict[str, int] = {}
    inv: dict[str, list[int]] = {}
    for n, snap in corpus.items():
        for s_ in {w[:5] for w in re.findall(r"[а-яё]{4,}", fold(" ".join(w["text"] for w in snap["words"])))}:
            df[s_] = df.get(s_, 0) + 1
            inv.setdefault(s_, []).append(n)
    index = {"pages_total": len(corpus), "df": df, "inv": {k: v for k, v in inv.items() if len(v) <= 40}, "ids": {}}

    def fake_scan(documents, kinds):
        return {int(d.id): ({"changes": {"entries": entries, "low_quality": []}} if "changes" in kinds else {"index": index}) for d in documents}

    with mock.patch("app.domain.free_hypotheses.doc_scan.scan_documents", side_effect=fake_scan), \
            mock.patch("app.domain.dataset_sources.extract_original_pages", side_effect=lambda d, pages: {p: corpus[p] for p in pages if p in corpus}):
        found, stats = change_detect.detect([pd_doc, rd_doc], gate, object_id="UNDMS-FIXTURE")
    result["change_sheet"] = {"entries_parsed": len(entries), "stats": stats,
                              "hypotheses": [{"description": h.description, "expected": h.expected, "actual": h.actual, "matched_by": h.details["matched_by"],
                                              "evidence": [(r.stage, r.page, r.bbox_pdf) for r in h.evidence]} for h in found]}
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
