"""Evidence tiers, no-result states and the explanation block of an evidence group (final round, step B1).

Experts (МГСУ review, oral feedback 16.10): finding a discrepancy matters more than classifying it. Three tiers of output:

  VIOLATION_CANDIDATE  значения сопоставлены, условие триггера параметра выполнено, оговорок нет.
  SUGGESTION           «предположение»: расхождение видно, но есть оговорка (редакция/привязка неоднозначны, триггер
                       нельзя проверить сравнением значений, порог матрицей не задан) -> проверяет инспектор.
  HYPOTHESIS           свободная гипотеза вне Матрицы (ТЗ 9.5; делает logical_analysis / free_hypotheses, не этот модуль).

VIOLATION_CANDIDATE and SUGGESTION are both `finding_status = CANDIDATE`: only the inspector sets CONFIRMED_VIOLATION.
The tier lives in `delta.evidence_tier`; it is written by the producer of the group (a SUGGESTION always carries
`delta.tier_reasons`) and defaulted here for every other CANDIDATE.

Everything here is pure (no DB, no files): `finalize_delta` is called by `evidence_groups.upsert_evidence_group` for every
producer, so all groups share one contract:

  delta.evidence_tier       for CANDIDATE groups
  delta.no_result_state     for MISSING_EVIDENCE / NOT_COMPARABLE / CLARIFICATION_REQUIRED / NEGATIVE_VERIFIED groups
  delta.explanation         {rule, threshold, threshold_basis, deviation_abs, deviation_pct, unit, text_confidence,
                            link_confidence, ...}: only what the pipeline computed, None otherwise. A probability of a
                            violation is never produced: no verifiable way to compute one exists.
"""
from __future__ import annotations

import re
from typing import Any

TIER_VIOLATION_CANDIDATE = "VIOLATION_CANDIDATE"
TIER_SUGGESTION = "SUGGESTION"
TIER_HYPOTHESIS = "HYPOTHESIS"
TIER_RANK = {TIER_VIOLATION_CANDIDATE: 0, TIER_SUGGESTION: 1, TIER_HYPOTHESIS: 2}
TIER_TITLES = {
    TIER_VIOLATION_CANDIDATE: "Нарушение-кандидат",
    TIER_SUGGESTION: "Предположение для верификации инспектором",
    TIER_HYPOTHESIS: "Свободная гипотеза (вне Матрицы)",
}

# --- no-result states (same strings as inspector_card.NO_*; the card derives them again at read time) ------------
NO_DOCUMENTS = "NO_DOCUMENTS"
NO_VALUE = "NO_VALUE_IN_DOCUMENT"
NOT_COMPARABLE = "NOT_COMPARABLE"
PROCESSING_ERROR = "PROCESSING_ERROR"
INCOMPLETE = "INCOMPLETE_CHECK"
NO_DIFFERENCE = "NO_DIFFERENCE"

_MISSING_DOCUMENT_REASONS = {"missing_stage", "missing_discipline_document"}
_PROCESSING_REASONS = {"garbled_table_text", "broken_text_layer_no_ocr", "attempts_exhausted", "worker_recovery", "LOW_QUALITY"}

# Why a SUGGESTION is a suggestion (machine readable; the protocol prints a text for each)
SUGGESTION_REASON_TEXT = {
    "revision_conflict": "в стадии несколько неупорядочиваемых редакций раздела: значение взято из одной из них",
    "trigger_not_evaluable": "условие триггера по каталогу нельзя проверить сравнением значений",
    "trigger_undetermined": "значения различаются, но форма/единица значения не позволяет проверить условие триггера",
    "threshold_not_set_by_matrix": "порог для этого уровня объекта матрицей не установлен; показана величина отклонения",
    "AMBIGUOUS_ROOM_MATCH": "помещение ПД нельзя однозначно сопоставить с помещением РД (нет этажа или номер повторяется)",
    "DUPLICATE_ROOM_KEY": "один и тот же ключ помещения описан несколькими записями",
    "AMBIGUOUS_SYSTEM_LINK": "подпись системы стоит между помещениями, привязка не назначена",
    "composition_change_not_decisive": "состав систем изменился (добавлены системы); правило параметра добавление нарушением не считает",
}


def no_result_state(status: str, reason: str) -> str | None:
    """The pipeline-level state of a result-less group (None for a candidate / confirmed violation / suspicion)."""
    status = str(status or "")
    reason = str(reason or "")
    if status in {"CANDIDATE", "CONFIRMED_VIOLATION", "SUSPICION"}:
        return None
    if status == "NEGATIVE_VERIFIED":
        return NO_DIFFERENCE
    if reason in _PROCESSING_REASONS:
        return PROCESSING_ERROR
    if status == "MISSING_EVIDENCE":
        return NO_DOCUMENTS if reason in _MISSING_DOCUMENT_REASONS else NO_VALUE
    if status == "NOT_COMPARABLE":
        return NOT_COMPARABLE
    if status == "CLARIFICATION_REQUIRED":
        return INCOMPLETE
    return INCOMPLETE


def reason_of(delta: dict[str, Any]) -> str:
    gate = delta.get("gate") if isinstance(delta.get("gate"), dict) else {}
    return str(delta.get("reason") or gate.get("reason") or "")


# --- tier ---------------------------------------------------------------------------------------------------------
def suggestion(delta: dict[str, Any], *reasons: str, **extra: Any) -> dict[str, Any]:
    """Marks `delta` as a SUGGESTION (in place) and returns it. At least one reason is required: a suggestion
    without a stated reservation would be a violation candidate in disguise."""
    names = [r for r in reasons if r]
    if not names:
        raise ValueError("a suggestion needs at least one reason")
    delta["evidence_tier"] = TIER_SUGGESTION
    delta["tier_reasons"] = names
    delta["tier_reason_text"] = [SUGGESTION_REASON_TEXT.get(r, r) for r in names]
    if extra:
        delta.setdefault("suggestion_basis", {}).update(extra)
    return delta


def tier_of(status: str, delta: dict[str, Any] | None) -> str | None:
    """Tier of a group: its stored tier, TIER_VIOLATION_CANDIDATE for any other CANDIDATE, the hypothesis tier for
    free-search groups, None for groups with no discrepancy to show."""
    delta = delta if isinstance(delta, dict) else {}
    status = str(status or "")
    if status == "SUSPICION" or str(delta.get("matrix_scope") or "").upper() == "FREE_SEARCH":
        return TIER_HYPOTHESIS
    if status in {"CANDIDATE", "CONFIRMED_VIOLATION"}:
        stored = delta.get("evidence_tier")
        return stored if stored in TIER_RANK else TIER_VIOLATION_CANDIDATE
    return None


_PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def review_sort_key(status: str, delta: dict[str, Any] | None, review_priority: str | None, confidence: float | None = None,
                    tie: Any = "") -> tuple:
    """Order of the inspector's queue: tier, then the parameter's review priority, then the larger relative deviation
    first (a tiny difference is read last), then a stable tie-break."""
    delta = delta if isinstance(delta, dict) else {}
    tier = tier_of(status, delta) or TIER_HYPOTHESIS
    explanation = delta.get("explanation") if isinstance(delta.get("explanation"), dict) else {}
    pct = explanation.get("deviation_pct")
    try:
        deviation = -float(pct) if pct is not None else 0.0
    except (TypeError, ValueError):
        deviation = 0.0
    return (TIER_RANK.get(tier, 3), _PRIORITY_RANK.get(str(review_priority or "").upper(), 1), deviation, str(tie))


# --- explanation --------------------------------------------------------------------------------------------------
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def _plain(value: Any) -> Any:
    """JSON-friendly number: Decimals as strings keep their digits, everything else is left alone."""
    try:
        from decimal import Decimal

        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
    except Exception:  # pragma: no cover
        pass
    return value


def _single_number(value: object) -> float | None:
    found = _NUMBER.findall(str(value or ""))
    if len(found) != 1:
        return None
    try:
        return float(found[0].replace(",", "."))
    except ValueError:
        return None


def _basis(param: Any) -> str | None:
    if param is None:
        return None
    parts = []
    text = str(getattr(param, "trigger_logic", None) or "").split("trigger=")[-1].strip()
    if text:
        parts.append(f"Каталог параметров, триггер: «{text}»")
    for attr, title in (("sp_reference", "СП"), ("gost_reference", "ГОСТ"), ("fz_reference", "ФЗ")):
        if getattr(param, attr, None):
            parts.append(f"{title}: {getattr(param, attr)}")
    return "; ".join(parts) or None


_CONFIDENCE_KINDS = (
    ("generic_", "эвристическая оценка привязки якоря к значению (константа механизма), не вероятность"),
    ("explication_compare", "эвристическая оценка сопоставления строк таблиц, не вероятность"),
    ("official_rule_pack", "минимум из измеренных оценок распознавания и привязки; где ничего не измерено — 0,5 («не измерено»)"),
)


def _confidence_kind(source: str) -> str | None:
    for prefix, text in _CONFIDENCE_KINDS:
        if source.startswith(prefix):
            return text
    return None


def build_explanation(param: Any, fields: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """The explanation contract of the card and the protocol. Producer-set values (`delta.explanation`) win over
    values derived here; whatever neither computed stays None. `text_confidence` (recognition: OCR word confidence;
    None for an exact PDF text layer) and `link_confidence` (assignment of a value to a place; a ranking score, not a
    probability) are never merged with each other or with the size of the deviation."""
    stored = delta.get("explanation") if isinstance(delta.get("explanation"), dict) else {}
    trigger = delta.get("trigger") if isinstance(delta.get("trigger"), dict) else {}
    threshold = stored.get("threshold")
    threshold_unit = stored.get("threshold_unit")
    if threshold is None and trigger.get("threshold") is not None:
        threshold = _plain(trigger.get("threshold"))
        threshold_unit = trigger.get("threshold_unit")
    rule = stored.get("rule") or trigger.get("label")
    basis = stored.get("threshold_basis") or _basis(param)
    dev_abs, dev_pct, dev_basis = stored.get("deviation_abs"), stored.get("deviation_pct"), stored.get("deviation_basis")
    if dev_basis is None:
        if trigger.get("change") is not None:
            dev_abs, dev_pct, dev_basis = _plain(trigger.get("change")), _plain(trigger.get("change_pct")), "правило триггера"
        else:
            left, right = _single_number(fields.get("expected_value")), _single_number(fields.get("actual_value"))
            if left is not None and right is not None and delta.get("evidence_tier") != TIER_HYPOTHESIS and not delta.get("set_difference"):
                dev_abs = round(right - left, 6)
                dev_pct = round(abs(right - left) / abs(left) * 100, 2) if left else None
                dev_basis = "разность извлечённых значений"
    unit = stored.get("unit") or (getattr(param, "unit", None) if param is not None else None)
    return {
        "rule": rule,
        "rule_kind": stored.get("rule_kind") or trigger.get("kind"),
        "threshold": threshold,
        "threshold_unit": threshold_unit,
        "threshold_basis": basis,
        "deviation_abs": dev_abs,
        "deviation_pct": dev_pct,
        "deviation_basis": dev_basis,
        "unit": unit,
        "text_confidence": stored.get("text_confidence", delta.get("text_confidence")),
        "link_confidence": stored.get("link_confidence", delta.get("link_confidence")),
        "probability": None,
        "combined_confidence": fields.get("confidence"),
        "combined_confidence_kind": _confidence_kind(str(delta.get("source") or "")),
        **{k: v for k, v in stored.items() if k not in {
            "rule", "rule_kind", "threshold", "threshold_unit", "threshold_basis", "deviation_abs", "deviation_pct",
            "deviation_basis", "unit", "text_confidence", "link_confidence", "probability"}},
    }


def finalize_delta(param: Any, fields: dict[str, Any]) -> dict[str, Any]:
    """`fields["delta"]` completed with tier / state / explanation. Returns a new dict; a group whose delta is not a
    dict is returned untouched."""
    delta = fields.get("delta")
    if not isinstance(delta, dict):
        return delta
    out = dict(delta)
    status = str(fields.get("finding_status") or "")
    if status == "CANDIDATE" or status == "CONFIRMED_VIOLATION":
        out["evidence_tier"] = tier_of(status, out)
        if out["evidence_tier"] == TIER_SUGGESTION and not out.get("tier_reasons"):
            out["tier_reasons"] = ["unspecified"]            # defensive: never an unexplained suggestion
        out["explanation"] = build_explanation(param, fields, out)
    elif status in {"NEGATIVE_VERIFIED", "MISSING_EVIDENCE", "NOT_COMPARABLE", "CLARIFICATION_REQUIRED"}:
        state = no_result_state(status, reason_of(out))
        if state:
            out["no_result_state"] = state
        if status == "NEGATIVE_VERIFIED":
            out["explanation"] = build_explanation(param, fields, out)
    return out
