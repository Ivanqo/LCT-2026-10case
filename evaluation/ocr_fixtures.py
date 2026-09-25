"""Adapter: turn the manually reviewed OCR pilot GOLD labels
(`evaluation/silver_labels/ocr_gold_review_pass1.jsonl`, produced by a human/vision-agent
character-by-character review of the organizer's SILVER OCR pilot -- see
`case_data/extracted/02_gold_methodology/ocr_pilot_20260811/`) into an
`evaluate_case10()`-shaped gold/predictions pair.

Predictions are produced by calling the actual production text-extraction path
(`dataset_sources.extract_original_pages`, falling back to `dataset_sources.ocr_page_snapshot`
exactly the way `official_rule_packs.py` chains them) against the same file+page+bbox as the
gold line, so `ocr_character_accuracy`/`key_field_exact_match` measure this system's own OCR,
not the SILVER-generation tool's.

This is the first pass that gives ТЗ 14.3's `ocr_character_accuracy` and `key_field_exact_match`
a real (non-null) denominator; see `CASE10_TZ_COMPLIANCE_AUDIT.md` section 1.4.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api_service"))

from app.domain.dataset_sources import extract_original_pages, ocr_page_snapshot  # noqa: E402

from evaluation.metrics import evaluate_case10

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLD_PATH = REPO_ROOT / "evaluation" / "silver_labels" / "ocr_gold_review_pass1.jsonl"
DEFAULT_KEY_FIELDS_PATH = REPO_ROOT / "evaluation" / "silver_labels" / "ocr_gold_key_field_examples_pass1.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _document_for(relative_path: str, sha256: str) -> SimpleNamespace:
    return SimpleNamespace(
        dataset_metadata={"document_manifest": {"relative_path": relative_path}},
        file_hash=sha256,
        content_hash=sha256,
    )


def _page_words(cache: dict, relative_path: str, sha256: str, page_number: int) -> list[dict[str, Any]]:
    """Same text-layer-first, OCR-fallback chain `official_rule_packs.py` uses in production."""
    key = (relative_path, sha256, page_number)
    if key not in cache:
        document = _document_for(relative_path, sha256)
        snapshots = extract_original_pages(document, [page_number])
        snapshot = snapshots.get(page_number)
        words = list(snapshot["words"]) if snapshot and snapshot.get("words") else None
        if not words:
            ocr_snapshot = ocr_page_snapshot(document, page_number)
            words = list(ocr_snapshot["words"]) if ocr_snapshot and ocr_snapshot.get("words") else []
        cache[key] = words
    return cache[key]


def _words_in_bbox(words: list[dict[str, Any]], bbox_pdf_pt: list[float], *, y_slack: float = 3.0, x_slack: float = 5.0) -> list[dict[str, Any]]:
    x1, y1, x2, y2 = bbox_pdf_pt
    selected = []
    for word in words:
        wx1, wy1, wx2, wy2 = word["bbox"]
        wcx, wcy = (wx1 + wx2) / 2.0, (wy1 + wy2) / 2.0
        if (y1 - y_slack) <= wcy <= (y2 + y_slack) and (x1 - x_slack) <= wcx <= (x2 + x_slack):
            selected.append(word)
    selected.sort(key=lambda w: w["bbox"][0])
    return selected


def predicted_text_for_line(cache: dict, row: dict[str, Any]) -> str:
    relative_path = row.get("source_relative_path")
    sha256 = row.get("source_sha256")
    page_number = row.get("pdf_page_number")
    bbox_pdf_pt = row.get("bbox_pdf_pt")
    if not relative_path or not sha256 or not page_number or not bbox_pdf_pt:
        return ""
    words = _page_words(cache, relative_path, sha256, int(page_number))
    matched = _words_in_bbox(words, bbox_pdf_pt)
    return " ".join(word["text"] for word in matched)


_DIGITS_RE = re.compile(r"\d+")
_DIGIT_VALUED_FIELDS = {"sheet", "room"}


def predicted_key_field_value(cache: dict, row: dict[str, Any], field: str) -> str | None:
    """Derives the predicted key-field value from the same predicted line text used for the
    OCR metric -- these 8 examples are ad-hoc stamp/footer fields (document code, sheet
    number, room number), not 132-matrix parameters, so there is no dedicated
    `official_rule_packs.py` extractor for them.

    `code` fields are compared as the full predicted line text (the gold value *is* the
    line, e.g. '01-07/22-14-П-АР1-кор1-ПЗ'). `sheet`/`room` fields are a single number
    embedded in a longer stamp string (e.g. 'комн.302' -> '302'), so the first digit run is
    pulled out instead of requiring an exact full-string match.
    """
    predicted_text = predicted_text_for_line(cache, row).strip()
    if field in _DIGIT_VALUED_FIELDS:
        match = _DIGITS_RE.search(predicted_text)
        return match.group(0) if match else None
    return predicted_text or None


def build_fixture(gold_path: Path = DEFAULT_GOLD_PATH, key_fields_path: Path = DEFAULT_KEY_FIELDS_PATH) -> dict[str, Any]:
    gold_rows = _read_jsonl(gold_path)
    by_line_id = {row["line_id"]: row for row in gold_rows}

    cache: dict = {}
    gold_ocr, predicted_ocr = [], []
    skipped_cannot_verify = []
    line_source_provenance: dict[str, str] = {}
    for row in gold_rows:
        if row.get("decision") == "CANNOT_VERIFY" or row.get("gold_text") is None:
            skipped_cannot_verify.append(row["line_id"])
            continue
        gold_ocr.append({"id": row["line_id"], "text": row["gold_text"]})
        predicted_ocr.append({"id": row["line_id"], "text": predicted_text_for_line(cache, row)})
        line_source_provenance[row["line_id"]] = row.get("source_provenance")

    gold_key_fields, predicted_key_fields = [], []
    key_field_rows = _read_jsonl(key_fields_path) if key_fields_path.exists() else []
    unresolved_key_fields = []
    for item in key_field_rows:
        source_row = by_line_id.get(item.get("source_line_id"))
        if not source_row:
            unresolved_key_fields.append(item["item_id"])
            continue
        gold_key_fields.append({"id": item["item_id"], "field": item.get("field"), "value": item.get("gold_value")})
        predicted_key_fields.append({"id": item["item_id"], "value": predicted_key_field_value(cache, source_row, item.get("field"))})

    gold = {
        "total_params": 0,
        "ocr": gold_ocr,
        "key_fields": gold_key_fields,
        "document_links": [],
        "findings": [],
        "evidence": [],
    }
    predictions = {
        "ocr": predicted_ocr,
        "key_fields": predicted_key_fields,
        "document_links": [],
        "findings": [],
        "evidence": [],
    }
    diagnostics = {
        "gold_path": str(gold_path),
        "key_fields_path": str(key_fields_path),
        "total_reviewed_lines": len(gold_rows),
        "scored_ocr_lines": len(gold_ocr),
        "skipped_cannot_verify_lines": len(skipped_cannot_verify),
        "skipped_cannot_verify_line_ids": skipped_cannot_verify,
        "scored_key_fields": len(gold_key_fields),
        "unresolved_key_fields": unresolved_key_fields,
    }
    return {
        "gold": gold,
        "predictions": predictions,
        "diagnostics": diagnostics,
        "line_source_provenance": line_source_provenance,
    }


def ocr_metrics_by_source_provenance(built: dict[str, Any]) -> dict[str, Any]:
    """Break `ocr_character_accuracy`/`ocr_wer`/`ocr_coverage` down by
    `source_provenance` (`PDF_TEXT_LAYER` vs `TESSERACT_RUS_ENG_PREANNOTATION`), so a
    change scoped to the OCR-fallback path (e.g. tesseract preprocessing/--psm) can be
    measured on its own subset without the much larger, unaffected text-layer subset
    diluting the signal."""
    provenance = built["line_source_provenance"]
    by_group: dict[str, dict[str, list]] = {}
    for gold_item, pred_item in zip(built["gold"]["ocr"], built["predictions"]["ocr"]):
        group = provenance.get(gold_item["id"]) or "UNKNOWN"
        bucket = by_group.setdefault(group, {"gold": [], "predictions": []})
        bucket["gold"].append(gold_item)
        bucket["predictions"].append(pred_item)
    result = {}
    for group, items in by_group.items():
        gold = {"total_params": 0, "ocr": items["gold"], "key_fields": [], "document_links": [], "findings": [], "evidence": []}
        predictions = {"ocr": items["predictions"], "key_fields": [], "document_links": [], "findings": [], "evidence": []}
        metrics = evaluate_case10(gold, predictions)
        result[group] = {
            "n": len(items["gold"]),
            "ocr_character_accuracy": metrics["ocr_character_accuracy"],
            "ocr_character_accuracy_confidence_interval_95": metrics["ocr_character_accuracy_confidence_interval_95"],
            "ocr_wer": metrics["ocr_wer"],
            "ocr_coverage": metrics["ocr_coverage"],
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD_PATH)
    parser.add_argument("--key-fields", type=Path, default=DEFAULT_KEY_FIELDS_PATH)
    parser.add_argument("--out", type=Path, default=None, help="optional path to dump the full metrics JSON")
    args = parser.parse_args()

    built = build_fixture(args.gold, args.key_fields)
    metrics = evaluate_case10(built["gold"], built["predictions"])
    report = {
        "diagnostics": built["diagnostics"],
        "metrics": metrics,
        "metrics_by_source_provenance": ocr_metrics_by_source_provenance(built),
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
