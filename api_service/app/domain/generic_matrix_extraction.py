"""Generic (data-driven, not per-code) numeric anchor extraction.

Covers the class of parameters whose catalog `parameter_name` is used, close
to verbatim, as a table-row label next to its own value in a real PD/RD/ID
document -- the common "ТЭП" (technical-economic indicators) table pattern
seen across ПЗ/СПЗУ sections. Unlike `official_rule_packs.py`'s five tuned
rule packs, this module has no per-parameter-code branches: it is driven
entirely by `Param.parameter_name` (the anchor phrase) and `Param.unit`
(eligibility gate, see `matrix_unit_classifier`).

This is intentionally conservative and lower-confidence than the tuned rule
packs: no gold-labelled example currently validates its precision (see
CASE10_MATRIX_132_COVERAGE.md), so every result it produces is tagged with a
distinct `model_version`/`extractor` and always lands on `CANDIDATE` (never
an auto-confirmed violation) pending inspector review, exactly like every
other evidence group in this pipeline.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Any, Iterable

from ..db.models import DocumentVersion, Param, SourceFragment
from .anchor_search import (
    GENERIC_SITE_LOCATION,
    build_semantic_query,
    expected_normative_references,
    extract_normative_references,
    extract_table_fingerprint,
    find_anchor_end_index_for_phrase,
    numbers_after,
    row_text_at_word,
    to_decimal,
)
from .cross_stage_localization import resolve_stage_round
from .matrix_unit_classifier import is_generic_anchor_eligible
from .value_plausibility import unit_evidence

GENERIC_EXTRACTOR_VERSION = "generic-anchor-table-v1"
GENERIC_EXTRACTOR_NAME = "generic_anchor_numeric"

# How many of the highest-confidence candidate pages (per parameter, per
# stage) are worth actually rendering. This mechanism shares the same
# locator signal the official rule packs use for page selection, which is a
# broad-recall/low-precision net (verified forensically -- see
# CASE10_MATRIX_132_COVERAGE.md) -- scanning more pages mostly adds cost, not
# new matches, so this is a modest bound rather than an exhaustive scan.
PAGES_PER_STAGE = 15
DEFAULT_PAGE_BUDGET = 1200

# How many extra words may appear between two consecutive anchor words (real
# tables insert a qualifier, e.g. catalog "Площадь здания" appearing as
# "Площадь жилого здания" on a residential project) -- verified against real
# document text while building this module, see CASE10_MATRIX_132_COVERAGE.md.
_MAX_ANCHOR_GAP_WORDS = 2

# How many words after the anchor are worth scanning for a value.
_MAX_LOOKAHEAD_WORDS = 25


def new_generic_budget(pages: int = DEFAULT_PAGE_BUDGET) -> dict[str, int]:
    return {"pages": pages}


@dataclass(slots=True)
class AnchorMatch:
    value: str
    normalized_value: str
    decimal_value: Decimal
    word_start: int
    word_end: int
    has_fraction: bool


def find_anchor_numeric_value(snapshot: dict[str, Any], anchor_phrase: str) -> AnchorMatch | None:
    """Find `anchor_phrase` (case-insensitive, tolerant of one inserted
    qualifier word) among `snapshot["words"]`, then return the best number
    shortly after it. "Best" prefers the nearest token carrying a decimal
    fraction (a real measured value) over a bare short integer, because
    multi-column PDF tables routinely reorder a "№" row-index column right
    next to the label once words are re-joined in reading order (verified
    forensically -- see CASE10_MATRIX_132_COVERAGE.md)."""
    words = snapshot.get("words") or []
    if not words:
        return None
    start_index = find_anchor_end_index_for_phrase(words, anchor_phrase, max_gap=_MAX_ANCHOR_GAP_WORDS)
    if start_index is None:
        return None
    tokens = numbers_after(words, start_index, _MAX_LOOKAHEAD_WORDS)
    if not tokens:
        return None
    fraction_tokens = [item for item in tokens if re.search(r"[.,]\d", item[0])]
    if fraction_tokens:
        best_text, first_idx, last_idx = min(fraction_tokens, key=lambda item: item[1])
        has_fraction = True
    else:
        multi_digit = [item for item in tokens if len(re.sub(r"\D", "", item[0])) >= 3]
        pool = multi_digit or tokens
        best_text, first_idx, last_idx = min(pool, key=lambda item: item[1])
        has_fraction = False
    decimal_value = to_decimal(best_text)
    if decimal_value is None:
        return None
    return AnchorMatch(
        value=best_text,
        normalized_value=str(decimal_value),
        decimal_value=decimal_value,
        word_start=first_idx,
        word_end=last_idx,
        has_fraction=has_fraction,
    )


def _word_range_bbox(words: list[dict[str, Any]], first_idx: int, last_idx: int) -> list[float] | None:
    boxes = [w["bbox"] for w in words[first_idx:last_idx + 1] if w.get("bbox")]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _context_window(words: list[dict[str, Any]], match: AnchorMatch, margin_words: int = 6) -> str:
    start = max(0, match.word_start - margin_words)
    end = min(len(words), match.word_end + 1 + margin_words)
    return " ".join(str(w.get("text") or "") for w in words[start:end])


@dataclass(slots=True)
class GenericObservation:
    value: str
    normalized_value: str
    decimal_value: Decimal
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
    # Word-level facts the comparability gate needs and only extraction can know (unit next to the number,
    # see value_plausibility.py / comparison_gate.py). Never part of the exported value.
    gate_facts: dict | None = None


def extract_generic_observation_for_page(
    parameter_name: str,
    document: DocumentVersion,
    page_number: int,
    snapshot: dict[str, Any],
    source_fragment: SourceFragment | None,
) -> GenericObservation | None:
    words = snapshot.get("words") or []
    match = find_anchor_numeric_value(snapshot, parameter_name)
    if match is None:
        return None
    bbox_pdf = _word_range_bbox(words, match.word_start, match.word_end)
    if bbox_pdf is None:
        return None
    width = float(snapshot.get("width") or 0) or 1.0
    height = float(snapshot.get("height") or 0) or 1.0
    bbox_normalized = [
        max(0.0, min(1.0, bbox_pdf[0] / width)),
        max(0.0, min(1.0, bbox_pdf[1] / height)),
        max(0.0, min(1.0, bbox_pdf[2] / width)),
        max(0.0, min(1.0, bbox_pdf[3] / height)),
    ]
    return GenericObservation(
        value=match.value,
        normalized_value=match.normalized_value,
        decimal_value=match.decimal_value,
        confidence=0.55 if match.has_fraction else 0.4,
        document=document,
        page=page_number,
        bbox_normalized=bbox_normalized,
        bbox_pdf=bbox_pdf,
        page_width=width,
        page_height=height,
        extractor=GENERIC_EXTRACTOR_NAME,
        context=_context_window(words, match),
        source_fragment=source_fragment,
    )


def values_equal(left: Decimal, right: Decimal) -> bool:
    """No domain-specific trigger threshold is available generically (that
    would require parsing free-text `trigger` phrasing per parameter, out of
    scope for this mechanism -- see backlog). A small absolute/relative
    tolerance only absorbs rounding/OCR jitter; anything larger is surfaced
    as CANDIDATE for a human to judge, never silently dropped."""
    tolerance = max(Decimal("0.01"), abs(left) * Decimal("0.001"))
    return abs(left - right) <= tolerance


def eligible_params(params: Iterable[Param], *, excluded_codes: frozenset[str]) -> list[Param]:
    return [
        param
        for param in params
        if is_generic_anchor_eligible(
            code=str(param.code), unit=param.unit, parameter_name=param.parameter_name, excluded_codes=excluded_codes,
        )
    ]


def _bucket_by_stage(
    params: list[Param],
    by_code: dict[str, list[SourceFragment]],
    by_id: dict[int, DocumentVersion],
    stage_codes: dict[str, str],
) -> dict[int, dict[str, list[SourceFragment]]]:
    result: dict[int, dict[str, list[SourceFragment]]] = {}
    for param in params:
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
            result[int(param.id)] = by_stage
    return result


def _match_numeric(
    snapshot: dict[str, Any], fragment: SourceFragment, doc: DocumentVersion, page: int, param: Param,
) -> tuple[GenericObservation, Any, str | None, bool, bool] | None:
    match = find_anchor_numeric_value(snapshot, str(param.parameter_name))
    if match is None:
        return None
    observation = extract_generic_observation_for_page(str(param.parameter_name), doc, page, snapshot, fragment)
    if observation is None:
        return None
    words = snapshot.get("words") or []
    fingerprint = extract_table_fingerprint(words, match.word_start)
    semantic_text = row_text_at_word(words, match.word_start)
    # Cross-stage-localization existence/correctness signals (see
    # `cross_stage_localization.py`'s module docstring, signal 4) -- real
    # forensic findings, LOS3A PZ-001/PZ-002. A value that reads as exactly
    # zero is, for every physical-dimension parameter this mechanism covers
    # (areas, volumes, counts of a structure that structurally exists), a
    # blank/unfilled template placeholder ("0,00") rather than a genuine
    # measurement far more often than it is a real zero -- deprioritized in
    # ranking, never dropped, so a parameter that genuinely IS zero still
    # wins when it is the only candidate. `normative_mismatch` compares the
    # normative document cited in the text AROUND the found value (same
    # window as `observation.context`, wide enough to reach a citation
    # wrapped onto its own line below the table label -- verified against
    # the real LOS3A PD document) against the parameter's own
    # catalog-declared reference; a no-op (both empty) until a catalog
    # source populates `Param.sp_reference`/`gost_reference`/`fz_reference`.
    is_empty_value = match.decimal_value == 0
    expected_refs = expected_normative_references(param)
    found_refs = extract_normative_references(observation.context)
    normative_mismatch = bool(expected_refs) and bool(found_refs) and expected_refs.isdisjoint(found_refs)
    observation.gate_facts = {
        "unit_evidence": unit_evidence(words, match.word_start, match.word_end, getattr(param, "unit", None), row_text=semantic_text),
    }
    return observation, fingerprint, semantic_text, is_empty_value, normative_mismatch


def collect_generic_observations(
    params: list[Param],
    by_code: dict[str, list[SourceFragment]],
    by_id: dict[int, DocumentVersion],
    *,
    stage_codes: dict[str, str],
    budget: dict[str, int],
) -> dict[int, dict[str, GenericObservation]]:
    """Two-round, PD-first pass over every eligible parameter (see
    `cross_stage_localization.py` for the shared engine and its rationale).
    Round 1 resolves PD exactly as before (top-`PAGES_PER_STAGE`-by-
    confidence, first page that parses wins). Round 2 resolves RD then ID,
    narrowing each parameter's own candidate pages toward documents sharing
    its resolved PD document's discipline and, when more than one candidate
    page independently parses, preferring the one whose table structure best
    matches PD's -- both purely additive: with no PD reference this reduces
    to the exact pre-existing behaviour. Returns {param_id: {stage:
    GenericObservation}}; a parameter absent from the result simply found
    nothing and falls through to the existing annotation-context abstention
    path, unchanged."""
    per_param_by_stage = _bucket_by_stage(params, by_code, by_id, stage_codes)
    if not per_param_by_stage:
        return {}
    params_by_id = {int(param.id): param for param in params}

    pd_entries = [
        (param_id, by_stage["PD"], None, None, params_by_id[param_id], build_semantic_query(params_by_id[param_id], "PD"))
        for param_id, by_stage in per_param_by_stage.items()
        if params_by_id[param_id].source_pd and "PD" in by_stage
    ]
    pd_results = resolve_stage_round(
        entries=pd_entries, by_id=by_id, pages_per_stage=PAGES_PER_STAGE, budget=budget, match_fn=_match_numeric,
    )

    later_entries = []
    for param_id, by_stage in per_param_by_stage.items():
        param = params_by_id[param_id]
        pd_candidate = pd_results.get(param_id)
        reference_document = pd_candidate.document if pd_candidate else None
        reference_fingerprint = pd_candidate.fingerprint if pd_candidate else None
        for stage, field in (("RD", "source_rd"), ("ID", "source_id")):
            if not getattr(param, field) or stage not in by_stage:
                continue
            later_entries.append(((param_id, stage), by_stage[stage], reference_document, reference_fingerprint, param, build_semantic_query(param, stage)))
    later_results = resolve_stage_round(
        entries=later_entries, by_id=by_id, pages_per_stage=PAGES_PER_STAGE, budget=budget, match_fn=_match_numeric,
    )

    result: dict[int, dict[str, GenericObservation]] = {}
    for param_id, candidate in pd_results.items():
        result.setdefault(param_id, {})["PD"] = candidate.payload
    for (param_id, stage), candidate in later_results.items():
        result.setdefault(param_id, {})[stage] = candidate.payload
    return {param_id: stages for param_id, stages in result.items() if stages}
