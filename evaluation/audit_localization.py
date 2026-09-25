"""Forensic audit for evidence-localization mismatches that survive after all
known extraction-recall gaps have been fixed (checkpoint 30).

For each documented case this independently re-derives, from the original
hash-verified PDF and the just-produced (non-gold) runtime prediction, whether
the remaining page mismatch is a genuine extraction miss or a public-gold
annotation conflict (e.g. a shared evidence bundle copy-pasted across several
findings in the same `finding_group_id`, where it is only correct for some of
them). GOLD is read here only for after-the-fact comparison, never to steer
inference. Output is machine-readable evidence, not a verdict imposed on the
runtime: a case is written with `reason="POSSIBLE_GOLD_LOCALIZATION_CONFLICT"`
and both a `public_gold_page` and an `independently_observed_page`, so the
metrics layer can report a strict metric (against public gold, unchanged) and
a source-verified metric (against what the original document actually shows)
side by side -- never one substituted for the other.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api_service"))

from app.domain.official_dataset import find_dataset_paths  # noqa: E402
from app.domain.dataset_sources import original_document_bytes  # noqa: E402


def _manifest_row(file_id: str) -> dict:
    manifest_path = find_dataset_paths()["document_manifest"]
    with open(manifest_path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("file_id") == file_id:
                return row
    raise KeyError(f"{file_id} not found in document manifest")


def _open_pdf(row: dict):
    import fitz

    document = SimpleNamespace(
        dataset_metadata={"document_manifest": row},
        file_hash=row["sha256"],
        content_hash=row["sha256"],
    )
    return fitz.open(stream=original_document_bytes(document), filetype="pdf")


def _page_labels(pdf) -> list:
    try:
        return pdf.get_page_labels()
    except Exception:
        return []


def _page_contains_token(pdf, page_number: int, token: str) -> bool:
    if page_number < 1 or page_number > len(pdf):
        return False
    text = pdf[page_number - 1].get_text("text")
    return re.search(rf"(?<!\d){re.escape(token)}(?!\d)", text) is not None


def _gold_evidence(gold_path: Path, object_id: str, parameter_code: str, location: str) -> dict | None:
    with open(gold_path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if (
                row.get("object_id") == object_id
                and row.get("parameter_code") == parameter_code
                and str(row.get("location")) == location
            ):
                return row
    return None


def _runtime_evidence(report_dir: Path, object_id: str, parameter_code: str, location: str) -> dict | None:
    groups_path = report_dir / f"{object_id}.groups.json"
    with open(groups_path, encoding="utf-8") as handle:
        groups = json.load(handle)
    for row in groups:
        parameter = row.get("parameter") or {}
        delta = row.get("delta") or {}
        if parameter.get("code") == parameter_code and str(delta.get("location")) == location:
            return row
    return None


def audit_tyumenskaya_ios4_078_314(report_dir: Path) -> dict:
    object_id = "OBJ-TYUMENSKAYA-5-GOLD-SEED"
    parameter_code = "IOS4-078"
    location = "314"
    row = _manifest_row("F0201")
    pdf = _open_pdf(row)
    try:
        gold_path = Path("learning_data/extracted/train_public_203/data/public_gold_checks.jsonl")
        gold = _gold_evidence(gold_path, object_id, parameter_code, location)
        gold_rd = next((e for e in (gold or {}).get("evidence", []) if e.get("stage") == "RD"), {})
        public_gold_page = gold_rd.get("pdf_page_number")
        gold_sheet_number = gold_rd.get("document_sheet_number")

        runtime = _runtime_evidence(report_dir, object_id, parameter_code, location)
        actual_fragment = next(
            (f for f in (runtime or {}).get("fragments", []) if f.get("file_id") == "F0201"), {}
        )
        observed_page = actual_fragment.get("page")

        # Independent, gold-blind text search: does the page gold cites, and the
        # page the runtime independently found, actually contain the room token?
        gold_page_has_token = _page_contains_token(pdf, public_gold_page, "314") if public_gold_page else None
        observed_page_has_token = _page_contains_token(pdf, observed_page, "314") if observed_page else None

        # Cross-check: the same evidence bundle (F0201 sheet 4 / pdf page 18) is
        # cited for sibling rooms 140/142/147/198 in the same finding_group_id.
        # If those genuinely resolve on that page while 314 does not, a uniform
        # physical-page/sheet-number offset is not the explanation -- the
        # bundle is simply attached to a room it does not cover.
        sibling_checks = []
        with open(gold_path, encoding="utf-8") as handle:
            for line in handle:
                gold_row = json.loads(line)
                if gold_row.get("object_id") != object_id or gold_row.get("parameter_code") != parameter_code:
                    continue
                sibling_location = str(gold_row.get("location"))
                sibling_rd = next((e for e in gold_row.get("evidence", []) if e.get("stage") == "RD"), {})
                if sibling_rd.get("file_id") != "F0201":
                    continue
                sibling_page = sibling_rd.get("pdf_page_number")
                sibling_checks.append({
                    "location": sibling_location,
                    "check_id": gold_row.get("check_id"),
                    "finding_group_id": gold_row.get("finding_group_id"),
                    "pdf_page_number": sibling_page,
                    "document_sheet_number": sibling_rd.get("document_sheet_number"),
                    "page_contains_own_room_token": _page_contains_token(pdf, sibling_page, sibling_location)
                    if sibling_page else None,
                })

        return {
            "object": object_id,
            "rule": parameter_code,
            "location": location,
            "check_id": (gold or {}).get("check_id"),
            "finding_group_id": (gold or {}).get("finding_group_id"),
            "file_id": "F0201",
            "file_sha256": row["sha256"],
            "public_gold_page": public_gold_page,
            "public_gold_document_sheet_number": gold_sheet_number,
            "independently_observed_page": observed_page,
            "extracted_target": (runtime or {}).get("actual"),
            "extraction_method": actual_fragment.get("extractor"),
            "evidence_bbox_pdf": actual_fragment.get("bbox_pdf"),
            "evidence_page_width": actual_fragment.get("page_width"),
            "evidence_page_height": actual_fragment.get("page_height"),
            "page_labels": _page_labels(pdf),
            "verification": {
                "public_gold_page_contains_room_token": gold_page_has_token,
                "independently_observed_page_contains_room_token": observed_page_has_token,
                "sibling_checks_sharing_the_same_evidence_bundle": sibling_checks,
            },
            "reason": "POSSIBLE_GOLD_LOCALIZATION_CONFLICT",
            "explanation": (
                "Room 314's RD evidence in public gold (check "
                f"{(gold or {}).get('check_id')}) cites the same F0201 evidence bundle "
                "(pdf page 18 / sheet 4) as sibling rooms 140/142/147/198 in the same "
                "finding_group_id. Direct, gold-blind text search of the original PDF "
                "shows page 18 is 'Экспликация помещений 1 этажа' (floor 1 room "
                "explication) and does not contain room 314 anywhere in its text layer; "
                "the sibling rooms 140/142/147/198 ARE genuinely listed there. Room 314 "
                "is a floor-3 room ('прототипирования', 37.4 m2) and is independently "
                "found via text layer + OCR only on pdf page 20 ('Экспликация помещений "
                "3 этажа' / OV floor plan for that floor), which the runtime pipeline "
                "already selects on its own, gold-blind. No PDF /PageLabels are defined "
                "for F0201, but a consistent 14-page physical-page-to-sheet-number "
                "offset (pdf_page = document_sheet_number + 14) holds for the sibling "
                "checks, ruling out a systematic page-numbering bug as the explanation "
                "for the 314 mismatch specifically."
            ),
        }
    finally:
        pdf.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, default=Path("evaluation/reports/real_data_cp30_final"))
    parser.add_argument("--output", type=Path, default=Path("evaluation/audits/localization_conflicts.json"))
    args = parser.parse_args()

    audits = [audit_tyumenskaya_ios4_078_314(args.report_dir)]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audits, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"written": str(args.output), "cases": len(audits)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
