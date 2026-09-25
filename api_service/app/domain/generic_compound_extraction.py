"""Generic (data-driven, not per-code) compound-unit anchor extraction.

Third sibling of `generic_matrix_extraction.py` (NUMERIC_SIMPLE) and
`generic_enum_extraction.py` (ENUM_CLASS): same anchor-phrase-then-nearby-
value shape, reusing `anchor_search.py`'s word-list matcher, but a compound
unit ("шт. / компл.", "м3/ч / Па / кВт") names N independently-comparable
numeric fields instead of one. Finds the anchor once, then parses N
consecutive numbers after it (in `Param.unit`'s own left-to-right order,
skipping "/" separator tokens when the source text literally repeats the
unit's own delimiter, e.g. "48 / 12") and compares PD vs RD/ID
**component-wise** -- a mismatch in any single component is enough to flag
`CANDIDATE`, even if the others agree, since a whole-string comparison could
silently average away a real per-field change.

Only ever eligible for compound units whose every component is itself a
plain numeric measurement (`matrix_unit_classifier.is_compound_eligible`) --
a compound unit with a categorical/text component (e.g. "Марка / Толщина",
"RAL / Артикул") is out of scope here, the same way ENUM_CLASS units are out
of scope for the numeric mechanism; see `matrix_unit_classifier.py`'s
`_component_looks_numeric` for why.

Like its siblings, conservative by construction: no gold-labelled example
currently validates most of these codes' precision (SPZU-029 is the
documented exception -- see CASE10_MATRIX_132_COVERAGE.md and
`evaluation/phase_d_validation.py`), so every result lands on `CANDIDATE`
(never an auto-confirmed violation) pending inspector review.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal
from typing import Any, Iterable

from ..db.models import DocumentVersion, Param, SourceFragment
from .anchor_search import (
    GENERIC_SITE_LOCATION,
    build_semantic_query,
    extract_table_fingerprint,
    find_anchor_end_index_for_phrase,
    numbers_after,
    row_text_at_word,
    to_decimal,
    union_bbox,
)
from .cross_stage_localization import resolve_stage_round
from .matrix_unit_classifier import compound_unit_components, is_compound_eligible
from .value_plausibility import component_unit_evidence

GENERIC_COMPOUND_EXTRACTOR_VERSION = "generic-anchor-compound-v1"
GENERIC_COMPOUND_EXTRACTOR_NAME = "generic_anchor_compound"

# Same order-of-magnitude bounds as the numeric/enum mechanisms.
PAGES_PER_STAGE = 15
DEFAULT_PAGE_BUDGET = 1200

# How many words after the anchor are worth scanning for the full set of
# components -- wider than the single-value numeric mechanism's own 25,
# since up to 3 separate numbers (plus stray unit/separator tokens between
# them) may need to appear before the last component is found.
_MAX_LOOKAHEAD_WORDS = 40


def new_compound_budget(pages: int = DEFAULT_PAGE_BUDGET) -> dict[str, int]:
    return {"pages": pages}


@dataclass(slots=True)
class CompoundMatch:
    raw_values: list[str]
    normalized_values: list[Decimal]
    word_start: int
    word_end: int
    # (first_word, last_word) of each selected number, in component order (unit-evidence gate).
    spans: list[tuple[int, int]] = dataclass_field(default_factory=list)


def find_anchor_compound_values(snapshot: dict[str, Any], anchor_phrase: str, component_count: int) -> CompoundMatch | None:
    """Find `anchor_phrase`, then greedily collect the next `component_count`
    numbers after it, in reading order -- skipping non-numeric tokens in
    between (unit labels, a literal "/" separator the source text may repeat
    from the catalog's own unit notation, stray punctuation). Fails (returns
    None) unless all `component_count` components are found: a partial match
    cannot be compared component-wise against the other stage's own partial
    match without risking a wrong pairing."""
    words = snapshot.get("words") or []
    if not words or component_count < 2:
        return None
    anchor_end = find_anchor_end_index_for_phrase(words, anchor_phrase)
    if anchor_end is None:
        return None
    tokens = numbers_after(words, anchor_end, _MAX_LOOKAHEAD_WORDS)
    if len(tokens) < component_count:
        return None
    selected = tokens[:component_count]
    decimals = [to_decimal(text) for text, _first, _last in selected]
    if any(value is None for value in decimals):
        return None
    return CompoundMatch(
        raw_values=[text for text, _first, _last in selected],
        normalized_values=decimals,
        word_start=selected[0][1],
        word_end=selected[-1][2],
        spans=[(first, last) for _text, first, last in selected],
    )


@dataclass(slots=True)
class CompoundObservation:
    raw_values: list[str]
    normalized_values: list[Decimal]
    component_labels: list[str]
    confidence: float
    document: DocumentVersion
    page: int
    bbox_normalized: list[float]
    bbox_pdf: list[float]
    page_width: float
    page_height: float
    extractor: str
    context: str
    source_fragment: SourceFragment | None
    # This mechanism never determines which room/construction element a
    # value belongs to (see GENERIC_SITE_LOCATION) -- fixed, not per-match.
    location: str = GENERIC_SITE_LOCATION
    # Optional LLM verifier verdict (llm_candidate_verifier.py, via
    # cross_stage_localization.resolve_stage_round) -- None unless
    # CASE10_LLM_VERIFIER_ENABLED; inspector-facing evidence only.
    llm_verification: dict | None = None
    # See generic_matrix_extraction.GenericObservation.gate_facts.
    gate_facts: dict | None = None

    @property
    def display_value(self) -> str:
        return " / ".join(self.raw_values)


def extract_compound_observation_for_page(
    parameter_name: str,
    component_labels: list[str],
    document: DocumentVersion,
    page_number: int,
    snapshot: dict[str, Any],
    source_fragment: SourceFragment | None,
) -> CompoundObservation | None:
    words = snapshot.get("words") or []
    match = find_anchor_compound_values(snapshot, parameter_name, len(component_labels))
    if match is None:
        return None
    bbox_pdf = union_bbox([w["bbox"] for w in words[match.word_start:match.word_end + 1] if w.get("bbox")])
    if bbox_pdf is None:
        return None
    width = float(snapshot.get("width") or 0) or 1.0
    height = float(snapshot.get("height") or 0) or 1.0
    margin = 6
    context_words = words[max(0, match.word_start - margin): match.word_end + margin + 1]
    return CompoundObservation(
        raw_values=match.raw_values,
        normalized_values=match.normalized_values,
        component_labels=component_labels,
        confidence=0.5,
        document=document,
        page=page_number,
        bbox_normalized=[
            max(0.0, min(1.0, bbox_pdf[0] / width)),
            max(0.0, min(1.0, bbox_pdf[1] / height)),
            max(0.0, min(1.0, bbox_pdf[2] / width)),
            max(0.0, min(1.0, bbox_pdf[3] / height)),
        ],
        bbox_pdf=bbox_pdf,
        page_width=width,
        page_height=height,
        extractor=GENERIC_COMPOUND_EXTRACTOR_NAME,
        context=" ".join(str(w.get("text") or "") for w in context_words),
        source_fragment=source_fragment,
    )


@dataclass(slots=True)
class ComponentComparison:
    label: str
    expected: str
    actual: str
    equal: bool


def compare_components(expected: CompoundObservation, actual: CompoundObservation) -> list[ComponentComparison]:
    """Component-wise, not whole-value, comparison -- a single differing
    field is a real finding even when every other field matches."""
    return [
        ComponentComparison(label=label, expected=str(expected_value), actual=str(actual_value), equal=values_close(expected_value, actual_value))
        for label, expected_value, actual_value in zip(
            expected.component_labels, expected.normalized_values, actual.normalized_values,
        )
    ]


def values_close(left: Decimal, right: Decimal) -> bool:
    tolerance = max(Decimal("0.01"), abs(left) * Decimal("0.001"))
    return abs(left - right) <= tolerance


def eligible_compound_params(params: Iterable[Param], *, excluded_codes: frozenset[str]) -> list[tuple[Param, list[str]]]:
    """Every eligible param paired with its own unit's component labels, in
    catalog order -- computed once so extraction never has to re-split the
    unit string per page."""
    out = []
    for param in params:
        if not is_compound_eligible(
            code=str(param.code), unit=param.unit, parameter_name=param.parameter_name, excluded_codes=excluded_codes,
        ):
            continue
        components = compound_unit_components(param.unit)
        if components:
            out.append((param, components))
    return out


def _match_compound(
    snapshot: dict[str, Any], fragment: SourceFragment, doc: DocumentVersion, page: int, ctx: tuple[Param, list[str]],
) -> tuple[CompoundObservation, Any, str | None] | None:
    param, components = ctx
    match = find_anchor_compound_values(snapshot, str(param.parameter_name), len(components))
    if match is None:
        return None
    observation = extract_compound_observation_for_page(str(param.parameter_name), components, doc, page, snapshot, fragment)
    if observation is None:
        return None
    words = snapshot.get("words") or []
    fingerprint = extract_table_fingerprint(words, match.word_start)
    semantic_text = row_text_at_word(words, match.word_start)
    observation.gate_facts = {
        "unit_evidence": [
            {"label": label, **component_unit_evidence(words, first, last, label, row_text=semantic_text)}
            for label, (first, last) in zip(components, match.spans)
        ],
    }
    return observation, fingerprint, semantic_text


def collect_compound_observations(
    params: list[Param],
    by_code: dict[str, list[SourceFragment]],
    by_id: dict[int, DocumentVersion],
    *,
    stage_codes: dict[str, str],
    budget: dict[str, int],
    excluded_codes: frozenset[str],
) -> dict[int, dict[str, CompoundObservation]]:
    """Mirrors `generic_matrix_extraction.collect_generic_observations`'s
    two-round, PD-first shape exactly (see `cross_stage_localization.py`),
    specialized for multi-component compound values."""
    param_components = eligible_compound_params(params, excluded_codes=excluded_codes)
    if not param_components:
        return {}
    components_by_id = {int(param.id): (param, components) for param, components in param_components}

    per_param_by_stage: dict[int, dict[str, list[SourceFragment]]] = {}
    for param, _components in param_components:
        candidates = by_code.get(str(param.code), [])
        if not candidates:
            continue
        by_stage: dict[str, list[SourceFragment]] = defaultdict(list)
        for fragment in candidates:
            doc = by_id.get(fragment.document_version_id)
            if not doc or not fragment.page:
                continue
            stage = doc.dataset_stage or stage_codes.get(doc.doc_stage)
            if stage in {"PD", "RD", "ID"}:
                by_stage[stage].append(fragment)
        if by_stage:
            per_param_by_stage[int(param.id)] = by_stage

    if not per_param_by_stage:
        return {}

    pd_entries = [
        (param_id, by_stage["PD"], None, None, components_by_id[param_id], build_semantic_query(components_by_id[param_id][0], "PD"))
        for param_id, by_stage in per_param_by_stage.items()
        if components_by_id[param_id][0].source_pd and "PD" in by_stage
    ]
    pd_results = resolve_stage_round(
        entries=pd_entries, by_id=by_id, pages_per_stage=PAGES_PER_STAGE, budget=budget, match_fn=_match_compound,
    )

    later_entries = []
    for param_id, by_stage in per_param_by_stage.items():
        param, components = components_by_id[param_id]
        pd_candidate = pd_results.get(param_id)
        reference_document = pd_candidate.document if pd_candidate else None
        reference_fingerprint = pd_candidate.fingerprint if pd_candidate else None
        for stage, field in (("RD", "source_rd"), ("ID", "source_id")):
            if not getattr(param, field) or stage not in by_stage:
                continue
            later_entries.append(((param_id, stage), by_stage[stage], reference_document, reference_fingerprint, (param, components), build_semantic_query(param, stage)))
    later_results = resolve_stage_round(
        entries=later_entries, by_id=by_id, pages_per_stage=PAGES_PER_STAGE, budget=budget, match_fn=_match_compound,
    )

    result: dict[int, dict[str, CompoundObservation]] = {}
    for param_id, candidate in pd_results.items():
        result.setdefault(param_id, {})["PD"] = candidate.payload
    for (param_id, stage), candidate in later_results.items():
        result.setdefault(param_id, {})[stage] = candidate.payload
    return {param_id: stages for param_id, stages in result.items() if stages}
