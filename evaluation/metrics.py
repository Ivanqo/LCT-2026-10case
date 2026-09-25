from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


_Z_95 = 1.959963984540054  # two-sided 95% normal quantile


def _wilson_interval(successes: float, trials: int, *, z: float = _Z_95) -> tuple[float, float]:
    """Wilson score interval, closed-form (no scipy dependency)."""
    n = float(trials)
    phat = max(0.0, min(1.0, float(successes) / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    center = phat + z2 / (2.0 * n)
    margin = z * ((phat * (1.0 - phat) / n + z2 / (4.0 * n * n)) ** 0.5)
    low = (center - margin) / denom
    high = (center + margin) / denom
    return (max(0.0, low), min(1.0, high))


def _metric_with_ci(successes: float, trials: int) -> dict[str, Any]:
    """Explicit sample size + 95% Wilson interval for a metric expressed as successes/trials.

    ТЗ 14.3 requires both alongside every point estimate; `trials == 0` (no
    atomic units to measure on) yields null for both rather than raising.
    """
    if not trials:
        return {"sample_size": None, "confidence_interval_95": None}
    successes = max(0.0, min(float(trials), float(successes)))
    low, high = _wilson_interval(successes, trials)
    return {"sample_size": int(trials), "confidence_interval_95": [low, high]}


POSITIVE_FINDING_STATUSES = {"CANDIDATE", "CONFIRMED_VIOLATION"}
ABSTAIN_FINDING_STATUSES = {
    "MISSING_EVIDENCE",
    "NOT_APPLICABLE",
    "NOT_COMPARABLE",
    "CLARIFICATION_REQUIRED",
    "ABSTAIN",
    "LOW_QUALITY",
    "SUSPICION",
}


@dataclass(frozen=True)
class BinaryCounts:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def with_case(self, *, expected: bool, actual: bool) -> "BinaryCounts":
        if expected and actual:
            return BinaryCounts(self.tp + 1, self.fp, self.tn, self.fn)
        if not expected and actual:
            return BinaryCounts(self.tp, self.fp + 1, self.tn, self.fn)
        if not expected and not actual:
            return BinaryCounts(self.tp, self.fp, self.tn + 1, self.fn)
        return BinaryCounts(self.tp, self.fp, self.tn, self.fn + 1)


def evaluate_case10(
    gold: dict[str, Any],
    predictions: dict[str, Any],
    *,
    bbox_iou_threshold: float = 0.5,
    localization_adjudications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate CASE10 predictions against a gold JSON fixture.

    `localization_adjudications` (typically loaded from
    `evaluation/audits/localization_conflicts.json`) never changes the strict,
    public-gold-only metrics -- it only adds a second, clearly-labeled
    `*_source_verified` metric alongside them, for documented cases where the
    public gold page itself is suspected of being wrong (see
    `evaluation/audit_localization.py`). The strict metric is what gates
    acceptance; the source-verified one is informational.
    """

    predictions = _align_predictions(gold, predictions)
    ocr = _evaluate_ocr(_index_by_id(gold.get("ocr")), _index_by_id(predictions.get("ocr")))
    key_fields = _evaluate_exact_match(_index_by_id(gold.get("key_fields")), _index_by_id(predictions.get("key_fields")), "value")
    document_links = _evaluate_exact_match(
        _index_by_id(gold.get("document_links")),
        _index_by_id(predictions.get("document_links")),
        "target_document_id",
    )
    adjudications_by_check = {
        str(row["check_id"]): row
        for row in (localization_adjudications or [])
        if row.get("check_id") and row.get("reason") == "POSSIBLE_GOLD_LOCALIZATION_CONFLICT"
    }
    evidence = _evaluate_evidence(
        _index_by_id(gold.get("evidence")),
        _index_by_id(predictions.get("evidence")),
        bbox_iou_threshold=bbox_iou_threshold,
        adjudications=adjudications_by_check,
    )
    findings = _evaluate_findings(gold.get("findings") or [], predictions.get("findings") or [])
    status_value = _evaluate_status_value(gold.get("findings") or [], predictions.get("findings") or [])
    split = _evaluate_object_level_split(gold, predictions)

    denominator = int(gold.get("total_params") or len(gold.get("findings") or []) or 0)
    predicted_finding_ids = {
        _parameter_key(row)
        for row in predictions.get("findings", [])
        if row.get("id") is not None and not _is_abstain_status(row.get("status"))
    }
    abstained_ids = {_parameter_key(row) for row in predictions.get("findings", []) if _is_abstain_status(row.get("status"))}
    abstained = len(abstained_ids - predicted_finding_ids)

    coverage = _safe_div(len(predicted_finding_ids), denominator) if denominator else None
    abstention_rate = _safe_div(abstained, denominator) if denominator else None

    metrics = {
        **ocr,
        "key_field_exact_match": key_fields["exact_match"],
        "key_field_exact_match_sample_size": key_fields["exact_match_sample_size"],
        "key_field_exact_match_confidence_interval_95": key_fields["exact_match_confidence_interval_95"],
        "document_linking_accuracy": document_links["exact_match"],
        "document_linking_accuracy_sample_size": document_links["exact_match_sample_size"],
        "document_linking_accuracy_confidence_interval_95": document_links["exact_match_confidence_interval_95"],
        **evidence,
        **findings["overall"],
        **status_value,
        "false_positive_rate": findings["overall"]["finding_false_positive_rate"],
        "false_positive_rate_sample_size": findings["overall"]["finding_false_positive_rate_sample_size"],
        "false_positive_rate_confidence_interval_95": findings["overall"]["finding_false_positive_rate_confidence_interval_95"],
        "coverage": coverage,
        "abstention_rate": abstention_rate,
        "localization_completeness": evidence["evidence_localization_accuracy"],
        **split,
        "counts": {
            "ocr_items": ocr["ocr_items"],
            "key_fields": key_fields["items"],
            "document_links": document_links["items"],
            "evidence_items": evidence["evidence_items"],
            "findings": findings["items"],
            "status_value_items": status_value["status_value_items"],
            "finding_confusion": findings["overall"]["counts"],
        },
        "per_category_metrics": findings["per_category_metrics"],
        "metric_denominator_reasons": gold.get("metric_denominator_reasons") or {},
        "quality_gates": {},
    }
    metrics["quality_gates"] = _quality_gates(metrics)
    return metrics


def _evaluate_ocr(gold_rows: dict[str, dict[str, Any]], predicted_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total_chars = 0
    total_char_errors = 0
    total_words = 0
    total_word_errors = 0
    covered = 0

    for item_id, gold_row in gold_rows.items():
        gold_text = str(gold_row.get("text") or "")
        predicted_text = str((predicted_rows.get(item_id) or {}).get("text") or "")
        if predicted_text.strip():
            covered += 1
        total_chars += len(gold_text)
        total_char_errors += _levenshtein(gold_text, predicted_text)
        gold_words = gold_text.split()
        predicted_words = predicted_text.split()
        total_words += len(gold_words)
        total_word_errors += _levenshtein_sequence(gold_words, predicted_words)

    ocr_cer = _safe_div(total_char_errors, total_chars) if total_chars else None
    ocr_wer = _safe_div(total_word_errors, total_words) if total_words else None
    character_ci = _metric_with_ci(max(0, total_chars - total_char_errors), total_chars)
    return {
        "ocr_cer": ocr_cer,
        "ocr_wer": ocr_wer,
        "ocr_character_accuracy": None if ocr_cer is None else max(0.0, 1.0 - ocr_cer),
        "ocr_character_accuracy_sample_size": character_ci["sample_size"],
        "ocr_character_accuracy_confidence_interval_95": character_ci["confidence_interval_95"],
        "ocr_coverage": _safe_div(covered, len(gold_rows)) if gold_rows else None,
        "ocr_items": len(gold_rows),
    }


def _evaluate_exact_match(gold_rows: dict[str, dict[str, Any]], predicted_rows: dict[str, dict[str, Any]], field: str) -> dict[str, Any]:
    matches = 0
    for item_id, gold_row in gold_rows.items():
        predicted_row = predicted_rows.get(item_id) or {}
        if _normalize_scalar(gold_row.get(field)) == _normalize_scalar(predicted_row.get(field)):
            matches += 1
    ci = _metric_with_ci(matches, len(gold_rows))
    return {
        "exact_match": _safe_div(matches, len(gold_rows)) if gold_rows else None,
        "exact_match_sample_size": ci["sample_size"],
        "exact_match_confidence_interval_95": ci["confidence_interval_95"],
        "items": len(gold_rows),
    }


def _evaluate_evidence(
    gold_rows: dict[str, dict[str, Any]],
    predicted_rows: dict[str, dict[str, Any]],
    *,
    bbox_iou_threshold: float,
    adjudications: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    adjudications = adjudications or {}
    page_matches = 0
    iou_sum = 0.0
    localized = 0
    localized_source_verified = 0
    adjudicated_applied = 0
    bbox_items = 0

    for item_id, gold_row in gold_rows.items():
        predicted_row = predicted_rows.get(item_id) or {}
        page_ok = (gold_row.get("page") is not None and predicted_row.get("page") is not None
                   and _normalize_scalar(gold_row.get("page")) == _normalize_scalar(predicted_row.get("page"))
                   and (not gold_row.get("file_id") or gold_row.get("file_id") == predicted_row.get("file_id")))
        if page_ok:
            page_matches += 1
        has_bbox = isinstance(gold_row.get("bbox"), (list, tuple)) and len(gold_row["bbox"]) == 4
        bbox_items += int(has_bbox)
        iou = bbox_iou(gold_row.get("bbox"), predicted_row.get("bbox")) if page_ok else 0.0
        iou_sum += iou
        strict_localized = bool(page_ok and (not has_bbox or iou >= bbox_iou_threshold))
        if strict_localized:
            localized += 1
        source_verified_localized = strict_localized
        if not source_verified_localized:
            case = adjudications.get(str(gold_row.get("finding_id") or ""))
            if case is not None:
                expected_page = _normalize_scalar(case.get("independently_observed_page"))
                expected_file = case.get("file_id")
                if (
                    expected_page
                    and expected_page == _normalize_scalar(predicted_row.get("page"))
                    and (not expected_file or expected_file == predicted_row.get("file_id"))
                ):
                    source_verified_localized = True
                    adjudicated_applied += 1
        if source_verified_localized:
            localized_source_verified += 1

    total = len(gold_rows)
    localization_ci = _metric_with_ci(localized, total)
    return {
        "evidence_page_accuracy": _safe_div(page_matches, total) if total else None,
        "source_localization_exact_file_page": _safe_div(page_matches, total) if total else None,
        "evidence_bbox_iou": _safe_div(iou_sum, bbox_items) if bbox_items else None,
        "evidence_bbox_items": bbox_items,
        "evidence_localization_accuracy": _safe_div(localized, total) if total else None,
        "evidence_localization_accuracy_sample_size": localization_ci["sample_size"],
        "evidence_localization_accuracy_confidence_interval_95": localization_ci["confidence_interval_95"],
        "evidence_localization_accuracy_strict_public_gold": _safe_div(localized, total) if total else None,
        "evidence_localization_accuracy_source_verified": _safe_div(localized_source_verified, total) if total else None,
        "evidence_localization_adjudicated_cases_applied": adjudicated_applied,
        "evidence_items": total,
    }


def _evaluate_findings(gold_rows: list[dict[str, Any]], predicted_rows: list[dict[str, Any]]) -> dict[str, Any]:
    predicted_by_id = _index_by_id(predicted_rows)
    counts = BinaryCounts()
    by_category: dict[str, BinaryCounts] = defaultdict(BinaryCounts)

    for gold_row in gold_rows:
        item_id = str(gold_row.get("id"))
        prediction = predicted_by_id.get(item_id) or {}
        expected = _is_expected_violation(gold_row)
        actual = _is_predicted_violation(prediction)
        counts = counts.with_case(expected=expected, actual=actual)
        category = str(gold_row.get("category") or gold_row.get("discipline") or "default")
        by_category[category] = by_category[category].with_case(expected=expected, actual=actual)

    gold_ids = {str(row.get("id")) for row in gold_rows}
    for prediction in predicted_rows:
        if str(prediction.get("id")) not in gold_ids and _is_predicted_violation(prediction):
            counts = counts.with_case(expected=False, actual=True)
            category = str(prediction.get("category") or "default")
            by_category[category] = by_category[category].with_case(expected=False, actual=True)

    return {
        "overall": _finding_metrics(counts),
        "per_category_metrics": {category: _finding_metrics(category_counts) for category, category_counts in sorted(by_category.items())},
        "items": len(gold_rows),
    }


def _evaluate_status_value(gold_rows: list[dict[str, Any]], predicted_rows: list[dict[str, Any]]) -> dict[str, Any]:
    predicted_by_id = _index_by_id(predicted_rows)
    comparable = 0
    matches = 0
    status_items = 0
    status_matches = 0
    value_items = 0
    value_matches = 0

    for gold_row in gold_rows:
        prediction = predicted_by_id.get(str(gold_row.get("id"))) or {}
        for field in ("protocol_status", "status", "comparison_result"):
            expected = gold_row.get(field)
            if expected in (None, ""):
                continue
            comparable += 1
            status_items += 1
            if _normalize_comparable_field(field, expected) == _normalize_comparable_field(field, prediction.get(field)):
                matches += 1
                status_matches += 1
        for field in ("normalized_value", "expected_value", "actual_value"):
            expected = gold_row.get(field)
            if expected in (None, ""):
                continue
            comparable += 1
            value_items += 1
            if _normalize_scalar(expected) == _normalize_scalar(prediction.get(field)):
                matches += 1
                value_matches += 1

    return {
        "normalized_value_and_status_accuracy": _safe_div(matches, comparable) if comparable else None,
        "protocol_status_accuracy": _safe_div(status_matches, status_items) if status_items else None,
        "normalized_value_accuracy": _safe_div(value_matches, value_items) if value_items else None,
        "status_value_items": comparable,
    }


def _evaluate_object_level_split(gold: dict[str, Any], predictions: dict[str, Any]) -> dict[str, Any]:
    conflicts = gold.get("object_split_conflicts") if isinstance(gold.get("object_split_conflicts"), dict) else {}
    object_splits = gold.get("object_splits") if isinstance(gold.get("object_splits"), dict) else {}
    predicted_objects = {
        str(row.get("object_id"))
        for row in predictions.get("findings", [])
        if row.get("object_id")
    }
    gold_objects = {str(row.get("object_id")) for row in gold.get("findings", []) if row.get("object_id")}
    known_objects = set(object_splits) | gold_objects | predicted_objects
    return {
        "object_level_split_valid": not bool(conflicts),
        "object_level_split_objects": len(known_objects),
        "object_level_split_conflicts": conflicts,
    }


def _finding_metrics(counts: BinaryCounts) -> dict[str, Any]:
    precision = _safe_div(counts.tp, counts.tp + counts.fp)
    recall = _safe_div(counts.tp, counts.tp + counts.fn)
    f1 = _safe_div(2 * counts.tp, 2 * counts.tp + counts.fp + counts.fn)
    fpr = _safe_div(counts.fp, counts.fp + counts.tn) if counts.tn else None
    precision_ci = _metric_with_ci(counts.tp, counts.tp + counts.fp)
    recall_ci = _metric_with_ci(counts.tp, counts.tp + counts.fn)
    f1_ci = _metric_with_ci(2 * counts.tp, 2 * counts.tp + counts.fp + counts.fn)
    fpr_ci = _metric_with_ci(counts.fp, counts.fp + counts.tn) if counts.tn else {"sample_size": None, "confidence_interval_95": None}
    return {
        "finding_precision": precision,
        "finding_precision_sample_size": precision_ci["sample_size"],
        "finding_precision_confidence_interval_95": precision_ci["confidence_interval_95"],
        "finding_recall": recall,
        "finding_recall_sample_size": recall_ci["sample_size"],
        "finding_recall_confidence_interval_95": recall_ci["confidence_interval_95"],
        "finding_f1": f1,
        "finding_f1_sample_size": f1_ci["sample_size"],
        "finding_f1_confidence_interval_95": f1_ci["confidence_interval_95"],
        "finding_detection_f1": f1,
        "finding_false_positive_rate": fpr,
        "finding_false_positive_rate_sample_size": fpr_ci["sample_size"],
        "finding_false_positive_rate_confidence_interval_95": fpr_ci["confidence_interval_95"],
        "counts": {"tp": counts.tp, "fp": counts.fp, "tn": counts.tn, "fn": counts.fn},
    }


def _quality_gates(metrics: dict[str, Any]) -> dict[str, Any]:
    gates = {
        "ocr_character_accuracy": (">=", 0.95),
        "key_field_exact_match": (">=", 0.90),
        "document_linking_accuracy": (">=", 0.95),
        "evidence_localization_accuracy": (">=", 0.95),
        "normalized_value_and_status_accuracy": (">=", 0.90),
        "finding_precision": (">=", 0.90),
        "finding_recall": (">=", 0.80),
        "finding_f1": (">=", 0.85),
        "false_positive_rate": ("<=", 0.10),
    }
    result: dict[str, Any] = {}
    for name, (operator, threshold) in gates.items():
        value = metrics.get(name)
        if value is None:
            passed = None
        elif operator == ">=":
            passed = float(value) >= threshold
        else:
            passed = float(value) <= threshold
        result[name] = {
            "value": value,
            "operator": operator,
            "threshold": threshold,
            "passed": passed,
            "reason": (metrics.get("metric_denominator_reasons") or {}).get(name) if value is None else None,
        }
    result["object_level_split"] = {
        "value": metrics.get("object_level_split_valid"),
        "operator": "required",
        "threshold": True,
        "passed": metrics.get("object_level_split_valid") if metrics.get("object_level_split_valid") is not None else None,
        "objects": metrics.get("object_level_split_objects"),
        "conflicts": metrics.get("object_level_split_conflicts") or {},
    }
    result["regression_gate"] = {
        "value": None,
        "operator": "max_degradation_pp",
        "threshold": 0.02,
        "passed": None,
        "reason": "baseline metrics are required to evaluate recall/FPR regression",
    }
    return result


def bbox_iou(left: Any, right: Any) -> float:
    try:
        ax1, ay1, ax2, ay2 = [float(value) for value in left]
        bx1, by1, bx2, by2 = [float(value) for value in right]
    except Exception:
        return 0.0
    ax1, ax2 = sorted((max(0.0, ax1), min(1.0, ax2)))
    ay1, ay2 = sorted((max(0.0, ay1), min(1.0, ay2)))
    bx1, bx2 = sorted((max(0.0, bx1), min(1.0, bx2)))
    by1, by2 = sorted((max(0.0, by1), min(1.0, by2)))
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = inter_w * inter_h
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    return 0.0 if union <= 0 else intersection / union


def _index_by_id(rows: Any) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("id") is not None:
            indexed[str(row["id"])] = row
    return indexed


def _is_expected_violation(row: dict[str, Any]) -> bool:
    if "is_violation" in row:
        return bool(row.get("is_violation"))
    return str(row.get("status") or "").upper() == "CONFIRMED_VIOLATION"


def _is_predicted_violation(row: dict[str, Any]) -> bool:
    if str(row.get("status") or "").upper() == "SUSPICION":
        return False
    return str(row.get("status") or "").upper() in POSITIVE_FINDING_STATUSES or bool(row.get("is_violation"))


def _is_abstain_status(status: Any) -> bool:
    return str(status or "").upper() in ABSTAIN_FINDING_STATUSES


def _normalize_scalar(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _normalize_comparable_field(field: str, value: Any) -> str:
    if field == "status":
        return _canonical_finding_status(value)
    if field == "comparison_result":
        return _canonical_comparison_result(value)
    return _normalize_scalar(value)


def _canonical_finding_status(value: Any) -> str:
    token = _normalize_token(value)
    if token in {"CANDIDATE", "CONFIRMED_VIOLATION", "VIOLATION_PRESENT"}:
        return "VIOLATION_PRESENT"
    if token in {"NEGATIVE_VERIFIED", "NO_VIOLATION", "NOT_APPLICABLE"}:
        return "NO_VIOLATION"
    if token in {"MISSING_EVIDENCE", "MISSING_DOCUMENT"}:
        return "MISSING_DOCUMENT"
    if token in {"NOT_COMPARABLE", "COMPARISON_IMPOSSIBLE"}:
        return "COMPARISON_IMPOSSIBLE"
    if token in ABSTAIN_FINDING_STATUSES:
        return "ABSTAIN"
    return token.lower()


def _canonical_comparison_result(value: Any) -> str:
    token = _normalize_token(value)
    if token in {
        "TRIGGERED",
        "VIOLATION_PRESENT",
        "MISSING_DESIGN_ELEMENT",
        "CONFIGURATION_MISMATCH",
        "VALUE_MISMATCH",
        "PARAMETER_MISMATCH",
    }:
        return "TRIGGERED"
    if token in {"NON_TRIGGERING", "NO_VIOLATION", "NEGATIVE_VERIFIED", "MATCH", "NO_CHANGE", "OK"}:
        return "NON_TRIGGERING"
    if token in {"MISSING_DOCUMENT", "MISSING_EVIDENCE"}:
        return "MISSING_DOCUMENT"
    if token in {"NOT_COMPARABLE", "COMPARISON_IMPOSSIBLE"}:
        return "COMPARISON_IMPOSSIBLE"
    return token.lower()


def _normalize_token(value: Any) -> str:
    return "_".join(str(value or "").strip().upper().replace("-", "_").split())


def _parameter_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("object_id") or ""), str(row.get("parameter_code") or row.get("id")))


def _finding_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    if not row.get("parameter_code") or not row.get("object_id"):
        return None
    return (str(row["object_id"]), str(row["parameter_code"]), _normalize_scalar(row.get("location")))


def _align_predictions(gold: dict[str, Any], predictions: dict[str, Any]) -> dict[str, Any]:
    """Match independently produced findings by object/parameter/location, never by label."""
    gold_rows = gold.get("findings") or []
    by_key = {_finding_key(row): row for row in gold_rows if _finding_key(row) is not None}
    gold_by_id = _index_by_id(gold_rows)
    id_map = {}
    matched = set()
    aligned = []
    for row in predictions.get("findings") or []:
        target = by_key.get(_finding_key(row)) if _finding_key(row) else gold_by_id.get(str(row.get("id")))
        target_id = str(target["id"]) if target else None
        if target_id and target_id not in matched:
            id_map[str(row["id"])] = target_id
            matched.add(target_id)
            aligned.append({**row, "id": target_id})
        else:
            aligned.append({**row, "id": "unmatched:" + str(row.get("id"))})
    source_evidence = predictions.get("evidence") or []
    aligned_evidence = []
    used = set()
    for expected in gold.get("evidence") or []:
        candidates = [(i, row) for i, row in enumerate(source_evidence) if i not in used and (
            id_map.get(str(row.get("finding_id"))) == str(expected.get("finding_id")) if expected.get("finding_id")
            else str(row.get("id")) == str(expected.get("id")))]
        if not candidates:
            continue
        index, best = max(candidates, key=lambda item: (
            item[1].get("file_id") == expected.get("file_id") and item[1].get("page") == expected.get("page"),
            item[1].get("stage") == expected.get("stage"),
        ))
        used.add(index)
        aligned_evidence.append({**best, "id": expected["id"]})
    source_links = predictions.get("document_links") or []
    aligned_links = []
    used_links = set()
    for expected in gold.get("document_links") or []:
        candidates = [(i, row) for i, row in enumerate(source_links) if i not in used_links and (
            id_map.get(str(row.get("finding_id"))) == str(expected.get("finding_id")) if expected.get("finding_id")
            else str(row.get("id")) == str(expected.get("id")))]
        if not candidates:
            continue
        index, best = max(candidates, key=lambda item: (
            item[1].get("target_document_id") == expected.get("target_document_id"),
            item[1].get("stage") == expected.get("stage"),
        ))
        used_links.add(index)
        aligned_links.append({**best, "id": expected["id"]})
    return {**predictions, "findings": aligned, "evidence": aligned_evidence, "document_links": aligned_links}


def _safe_div(numerator: float | int, denominator: float | int) -> float | None:
    if not denominator:
        return None
    return float(numerator) / float(denominator)


def _levenshtein(left: str, right: str) -> int:
    return _levenshtein_sequence(list(left), list(right))


def _levenshtein_sequence(left: list[Any], right: list[Any]) -> int:
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_value in enumerate(left, start=1):
        current = [i]
        for j, right_value in enumerate(right, start=1):
            cost = 0 if left_value == right_value else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]
