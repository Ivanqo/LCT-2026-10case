"""Pure, dependency-free classification of the 132-parameter catalog's `unit`
column into families relevant to automated extraction.

Deliberately has no imports beyond the standard library so it can be loaded
both from inside the API service (`app.domain.generic_matrix_extraction`) and
standalone from `evaluation/matrix_coverage.py` (which otherwise avoids any
coupling to the API service's package/settings machinery) without either
side needing the other's runtime environment.
"""
from __future__ import annotations

import re

# Units denoting a single, directly comparable physical quantity: exactly one
# number per value, one axis of comparison. Compound units ("м3/ч / Па / кВт"),
# counts ("шт."), and categorical codes ("Марка", "Класс (А)") are excluded on
# purpose -- see classify_unit() -- because they need either multi-value
# parsing or a small domain vocabulary this module does not have, and a
# best-effort guess there is more likely to mislead an inspector than a clear
# MISSING_EVIDENCE/NOT_COMPARABLE abstention.
NUMERIC_SIMPLE_UNITS = {"м", "мм", "м2", "м²", "м3", "м³", "%", "мм2", "мм²", "‰"}
NUMERIC_COUNT_UNITS = {"шт.", "ед.", "чел.", "дни", "мин", "компл."}
_ENUM_MARKERS = ("марка", "класс", "букв", "кат.", "степень", "статус")
_EMPTY_UNIT_MARKERS = {"", "—", "-", "–"}

_MIN_ANCHOR_WORDS = 2
_MIN_ANCHOR_CHARS = 8
_WORD_RE = re.compile(r"\S+")


def classify_unit(unit: str | None) -> str:
    """Classify a raw `unit` string from parameter_catalog_132.jsonl.

    Returns one of: NONE, NUMERIC_SIMPLE, NUMERIC_COMPOUND, NUMERIC_COUNT,
    ENUM_CLASS, OTHER. Used both to decide generic-extractor eligibility at
    runtime and to label every one of the 132 parameters in the coverage
    report -- the same function backs both, so the two never drift apart.
    """
    text = str(unit or "").strip()
    if text in _EMPTY_UNIT_MARKERS:
        return "NONE"
    if text in NUMERIC_SIMPLE_UNITS:
        return "NUMERIC_SIMPLE"
    if "/" in text:
        return "NUMERIC_COMPOUND"
    lowered = text.lower()
    if lowered in NUMERIC_COUNT_UNITS:
        return "NUMERIC_COUNT"
    if any(marker in lowered for marker in _ENUM_MARKERS):
        return "ENUM_CLASS"
    return "OTHER"


def normalize_anchor_text(text: str) -> str:
    lowered = str(text or "").lower().replace("ё", "е")  # ё -> е
    lowered = re.sub(r"[.,;:]+$", "", lowered.strip())
    return re.sub(r"\s+", " ", lowered).strip()


def is_generic_anchor_eligible(*, code: str, unit: str | None, parameter_name: str | None, excluded_codes: frozenset[str]) -> bool:
    """A parameter qualifies for the generic anchor+number extractor when
    (a) it is not already served by a tuned, gold-tested official rule pack,
    (b) its unit is a single physical quantity (NUMERIC_SIMPLE), and
    (c) its catalog name is a distinctive enough anchor phrase (>=2 words,
    >=8 characters) that a literal-phrase search in real page text is
    unlikely to collide with an unrelated mention. This is a data-driven
    gate, not a per-code list: any future catalog parameter with a plain
    physical unit and a multi-word name automatically qualifies."""
    if code in excluded_codes:
        return False
    if classify_unit(unit) != "NUMERIC_SIMPLE":
        return False
    return _has_distinctive_anchor_name(parameter_name)


def _has_distinctive_anchor_name(parameter_name: str | None) -> bool:
    name = normalize_anchor_text(parameter_name or "")
    if len(name) < _MIN_ANCHOR_CHARS:
        return False
    if len(_WORD_RE.findall(name)) < _MIN_ANCHOR_WORDS:
        return False
    return True


def is_enum_class_eligible(*, code: str, unit: str | None, parameter_name: str | None, excluded_codes: frozenset[str]) -> bool:
    """A parameter qualifies for the generic enum-class extractor
    (`generic_enum_extraction.py`) when it is not already a tuned rule pack,
    its unit is a categorical code (ENUM_CLASS), and its own name/section
    resolves to at least one small controlled vocabulary family in
    `enum_class_vocabularies.py`. A parameter with no matching family (e.g.
    "Марка" for pipe material, "Статус" for an external-system registration
    check -- neither has a small closed normative vocabulary this module can
    build without guessing) stays ineligible/CONFIG_ONLY rather than forcing
    a best-effort match."""
    if code in excluded_codes:
        return False
    if classify_unit(unit) != "ENUM_CLASS":
        return False
    from .enum_class_vocabularies import resolve_enum_families  # local import: keeps this module's own import graph unit-only until enum eligibility is actually checked

    return bool(resolve_enum_families(parameter_name=parameter_name))


_COMPOUND_SPLIT_RE = re.compile(r"\s+/\s+")


def compound_unit_components(unit: str | None) -> list[str] | None:
    """Splits a genuinely multi-field compound unit ("шт. / компл.", "RAL /
    Артикул", "м³/ч / м / кВт") into its component labels, in catalog order.

    Only a "/" surrounded by whitespace on both sides separates independent
    components -- a bare "/" with no surrounding spaces ("м³/ч", "л/с",
    "Вт/(м·С)") is a single physical quantity's own rate/ratio notation, not
    two comparable values, and must never be split. Returns None for a unit
    that does not contain any " / "-separated component (whether or not
    `classify_unit` still calls it NUMERIC_COMPOUND on the cruder bare-"/"
    test) -- callers use this, not `classify_unit`, to gate real component-
    wise extraction eligibility."""
    text = str(unit or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in _COMPOUND_SPLIT_RE.split(text)]
    parts = [part for part in parts if part]
    return parts if len(parts) >= 2 else None


# Component labels that mean "this slot holds a categorical code/text value,
# not a number" -- a compound unit with any such component (e.g. "Марка /
# Толщина", "RAL / Артикул", "т / Класс") is excluded from the generic
# compound extractor: parsing a reliable text-token value per component
# without a curated per-family vocabulary (unlike ENUM_CLASS, see
# `enum_class_vocabularies.py`) would risk grabbing the wrong word (e.g. the
# component's own label text) rather than failing safely. Reuses the same
# marker words `classify_unit` already treats as enum signals, plus two
# catalog-specific text-code labels that are not measurement units.
_TEXT_COMPONENT_MARKERS = _ENUM_MARKERS + ("ral", "артикул")


def _component_looks_numeric(component_label: str) -> bool:
    lowered = component_label.strip().lower()
    return not any(marker in lowered for marker in _TEXT_COMPONENT_MARKERS)


def is_compound_eligible(*, code: str, unit: str | None, parameter_name: str | None, excluded_codes: frozenset[str]) -> bool:
    """A parameter qualifies for the generic compound extractor
    (`generic_compound_extraction.py`) when it is not already a tuned rule
    pack, `compound_unit_components` finds >=2 genuine components in its
    unit, EVERY component looks like a numeric measurement (not a
    categorical code slot -- see `_component_looks_numeric`), and its name
    is still a distinctive enough anchor phrase (same bar as the numeric
    mechanism)."""
    if code in excluded_codes:
        return False
    if classify_unit(unit) != "NUMERIC_COMPOUND":
        return False
    components = compound_unit_components(unit)
    if components is None or not all(_component_looks_numeric(part) for part in components):
        return False
    return _has_distinctive_anchor_name(parameter_name)


# NUMERIC_COUNT-classified codes where "count the content rows of a real
# specification/exposition/schedule table" is the semantically correct
# reading of the parameter (verified by reading each parameter_name/trigger
# against its own source_pd/source_rd catalog text -- not from organizer
# gold, which this module never reads). Deliberately NOT every NUMERIC_COUNT
# code: PZ-007 ("Этажность")/PZ-013 ("Технологическая мощность") are single
# ТЭП-table scalars, not enumerable tables; POS-086 ("Максимальная
# численность") needs the PEAK value of a headcount-over-time schedule, not
# a row count; POS-082 ("Продолжительность этапов") needs a specific stage's
# duration, not a count of stage rows; PPM-103 ("Пределы огнестойкости...")
# needs a per-door EI-rating threshold comparison, not a count. Applying a
# naive row-count to any of these five would not fail safely (an honest
# MISSING_EVIDENCE) -- it risks a confidently wrong CANDIDATE/NEGATIVE_
# VERIFIED finding from counting the wrong thing. See
# CASE10_MATRIX_132_COVERAGE.md for the per-code rationale.
TABLE_ROW_COUNT_SEMANTIC_CODES = frozenset({
    "PZ-010", "PZ-011", "PZ-012", "SPZU-037", "SPZU-038", "ODI-120", "PPM-110", "ZU-129",
})


def is_table_count_eligible(*, code: str, unit: str | None, excluded_codes: frozenset[str]) -> bool:
    """A parameter qualifies for the generic table-row-count extractor
    (`generic_table_row_count.py`) when it is not already a tuned rule pack,
    its unit is NUMERIC_COUNT, and it is on the explicit semantic allowlist
    above -- a data-driven gate would over-claim here (unit alone cannot
    distinguish "count these rows" from "take the peak/duration/threshold
    reading off this table"), so this one dimension is a maintained list
    instead of a keyword heuristic."""
    if code in excluded_codes:
        return False
    if classify_unit(unit) != "NUMERIC_COUNT":
        return False
    return code in TABLE_ROW_COUNT_SEMANTIC_CODES
