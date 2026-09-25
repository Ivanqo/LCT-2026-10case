"""Generic (data-driven, not per-code) enum-class anchor extraction.

Sibling of `generic_matrix_extraction.py` (which covers NUMERIC_SIMPLE
parameters): same anchor-phrase-then-nearby-value shape, reusing
`anchor_search.py`'s word-list matcher, but the "value" is a categorical
code (concrete/steel/rebar grade, fire-hazard/energy-efficiency class, ...)
looked up against `enum_class_vocabularies.py`'s small controlled
vocabularies instead of a bare number. Comparison is exact canonical-value
equality -- never a numeric tolerance -- per the vocabulary's own class
identity.

Like the numeric mechanism, this is intentionally conservative: no
gold-labelled example currently validates its precision (see
CASE10_MATRIX_132_COVERAGE.md), so every result lands on `CANDIDATE` (never
an auto-confirmed violation) pending inspector review, tagged with its own
`model_version`/`extractor` so it is never confused with a tuned rule pack.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from ..db.models import DocumentVersion, Param, SourceFragment
from .anchor_search import (
    GENERIC_SITE_LOCATION,
    build_semantic_query,
    extract_table_fingerprint,
    find_anchor_end_index_for_phrase,
    indexed_words,
    row_text_at_word,
    word_index_at,
)
from .cross_stage_localization import resolve_stage_round
from .enum_class_vocabularies import EnumFamily, find_enum_value, resolve_enum_families
from .matrix_unit_classifier import is_enum_class_eligible

GENERIC_ENUM_EXTRACTOR_VERSION = "generic-anchor-enum-v1"
GENERIC_ENUM_EXTRACTOR_NAME = "generic_anchor_enum"

# Same order-of-magnitude bounds as the numeric mechanism -- see
# generic_matrix_extraction.py for the forensic basis (broad-recall/
# low-precision upstream locator signal, more page budget does not add
# recall beyond this).
PAGES_PER_STAGE = 15
DEFAULT_PAGE_BUDGET = 1200

# How far past the anchor phrase (in characters of the joined page text) a
# class-code token may still plausibly belong to it -- long enough to skip a
# "№"/row-index cell and a unit label, short enough that an unrelated later
# table row's own class code is not swept in.
_LOOKAHEAD_CHARS = 220


def new_enum_budget(pages: int = DEFAULT_PAGE_BUDGET) -> dict[str, int]:
    return {"pages": pages}


@dataclass(slots=True)
class EnumMatch:
    canonical_value: str
    family: str
    word_index: int


def find_anchor_enum_value(snapshot: dict[str, Any], anchor_phrase: str, family: EnumFamily) -> EnumMatch | None:
    """Find `anchor_phrase` (case-insensitive, same qualifier-tolerant match
    as the numeric mechanism) among `snapshot["words"]`, then look for
    `family`'s class-code pattern shortly afterwards."""
    words = snapshot.get("words") or []
    if not words:
        return None
    anchor_end = find_anchor_end_index_for_phrase(words, anchor_phrase)
    if anchor_end is None:
        return None
    text, indexed, offsets = indexed_words(words[anchor_end:])
    window = text[:_LOOKAHEAD_CHARS]
    found = find_enum_value(window, family)
    if found is None:
        return None
    canonical_value, start_char, _end_char = found
    local_index = word_index_at(offsets, start_char)
    if local_index is None:
        return None
    return EnumMatch(canonical_value=canonical_value, family=family.name, word_index=anchor_end + local_index)


@dataclass(slots=True)
class EnumObservation:
    value: str
    canonical_value: str
    family: str
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


def extract_enum_observation_for_page(
    parameter_name: str,
    family: EnumFamily,
    document: DocumentVersion,
    page_number: int,
    snapshot: dict[str, Any],
    source_fragment: SourceFragment | None,
) -> EnumObservation | None:
    words = snapshot.get("words") or []
    match = find_anchor_enum_value(snapshot, parameter_name, family)
    if match is None:
        return None
    word = words[match.word_index]
    bbox_pdf = list(map(float, word["bbox"]))
    width = float(snapshot.get("width") or 0) or 1.0
    height = float(snapshot.get("height") or 0) or 1.0
    margin = 6
    context_words = words[max(0, match.word_index - margin): match.word_index + margin + 1]
    return EnumObservation(
        value=str(word.get("text") or match.canonical_value),
        canonical_value=match.canonical_value,
        family=match.family,
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
        extractor=GENERIC_ENUM_EXTRACTOR_NAME,
        context=" ".join(str(w.get("text") or "") for w in context_words),
        source_fragment=source_fragment,
    )


def enum_values_equal(left: str, right: str) -> bool:
    return left == right


def eligible_enum_params(params: Iterable[Param], *, excluded_codes: frozenset[str]) -> list[tuple[Param, EnumFamily]]:
    """Every eligible param paired with the (first) vocabulary family it
    resolves to -- computed once so the extraction pass below never has to
    re-resolve it per page."""
    out = []
    for param in params:
        if not is_enum_class_eligible(
            code=str(param.code), unit=param.unit, parameter_name=param.parameter_name, excluded_codes=excluded_codes,
        ):
            continue
        families = resolve_enum_families(parameter_name=param.parameter_name, pd_section=getattr(param, "section", None))
        if families:
            out.append((param, families[0]))
    return out


def _match_enum(
    snapshot: dict[str, Any], fragment: SourceFragment, doc: DocumentVersion, page: int, ctx: tuple[Param, EnumFamily],
) -> tuple[EnumObservation, Any, str | None] | None:
    param, family = ctx
    match = find_anchor_enum_value(snapshot, str(param.parameter_name), family)
    if match is None:
        return None
    observation = extract_enum_observation_for_page(str(param.parameter_name), family, doc, page, snapshot, fragment)
    if observation is None:
        return None
    words = snapshot.get("words") or []
    fingerprint = extract_table_fingerprint(words, match.word_index)
    semantic_text = row_text_at_word(words, match.word_index)
    return observation, fingerprint, semantic_text


def collect_enum_observations(
    params: list[Param],
    by_code: dict[str, list[SourceFragment]],
    by_id: dict[int, DocumentVersion],
    *,
    stage_codes: dict[str, str],
    budget: dict[str, int],
    excluded_codes: frozenset[str],
) -> dict[int, dict[str, EnumObservation]]:
    """Mirrors `generic_matrix_extraction.collect_generic_observations`'s
    two-round, PD-first shape exactly (see `cross_stage_localization.py`),
    specialized for enum-class values instead of numbers."""
    param_families = eligible_enum_params(params, excluded_codes=excluded_codes)
    if not param_families:
        return {}
    families_by_id = {int(param.id): (param, family) for param, family in param_families}

    per_param_by_stage: dict[int, dict[str, list[SourceFragment]]] = {}
    for param, _family in param_families:
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
        (param_id, by_stage["PD"], None, None, families_by_id[param_id], build_semantic_query(families_by_id[param_id][0], "PD"))
        for param_id, by_stage in per_param_by_stage.items()
        if families_by_id[param_id][0].source_pd and "PD" in by_stage
    ]
    pd_results = resolve_stage_round(
        entries=pd_entries, by_id=by_id, pages_per_stage=PAGES_PER_STAGE, budget=budget, match_fn=_match_enum,
    )

    later_entries = []
    for param_id, by_stage in per_param_by_stage.items():
        param, family = families_by_id[param_id]
        pd_candidate = pd_results.get(param_id)
        reference_document = pd_candidate.document if pd_candidate else None
        reference_fingerprint = pd_candidate.fingerprint if pd_candidate else None
        for stage, field in (("RD", "source_rd"), ("ID", "source_id")):
            if not getattr(param, field) or stage not in by_stage:
                continue
            later_entries.append(((param_id, stage), by_stage[stage], reference_document, reference_fingerprint, (param, family), build_semantic_query(param, stage)))
    later_results = resolve_stage_round(
        entries=later_entries, by_id=by_id, pages_per_stage=PAGES_PER_STAGE, budget=budget, match_fn=_match_enum,
    )

    result: dict[int, dict[str, EnumObservation]] = {}
    for param_id, candidate in pd_results.items():
        result.setdefault(param_id, {})["PD"] = candidate.payload
    for (param_id, stage), candidate in later_results.items():
        result.setdefault(param_id, {})[stage] = candidate.payload
    return {param_id: stages for param_id, stages in result.items() if stages}
