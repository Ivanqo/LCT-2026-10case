"""Shared cross-stage page-localization engine for the four generic
(data-driven, not per-code) extraction mechanisms (`generic_matrix_
extraction.py`, `generic_enum_extraction.py`, `generic_compound_
extraction.py`, `generic_table_row_count.py`).

Every one of the four mechanisms shared the exact same bottleneck (see
CASE10_MATRIX_132_COVERAGE.md, checkpoints 35/38): once a PD-stage value is
found, the RD/ID candidate pages for the *same* parameter code are picked
purely by the upstream `learning_annotation` keyword-tagger's own
`confidence` score -- a broad-recall/low-precision signal that routinely
fails to put the real RD page inside the top `PAGES_PER_STAGE` window, and
never distinguishes "this page's table really looks like PD's table" from
"this page happens to mention the same phrase". This module adds two
independent, additive signals on top of that existing ranking, both scoped
to *within* the same fixed per-stage page budget (never touching
`PAGES_PER_STAGE` itself):

1. **Document-manifest narrowing** (`narrow_candidate_fragments`): once a
   PD-stage document is resolved for a parameter, RD/ID candidates whose own
   document shares its `discipline`/`dataset_section` are ranked ahead of
   the rest -- using the document registry CASE10 already populates at
   import time (`official_dataset.py::_upsert_document_version`), not a new
   data source. Still capped at the existing `PAGES_PER_STAGE`: when the
   narrow pool has fewer candidates than that, the remaining slots are
   filled from the wider pool exactly as before.

2. **Structural table fingerprint** (`anchor_search.extract_table_fingerprint`
   / `fingerprint_similarity`): among the pages actually rendered for a
   stage, if more than one independently yields a value, the one whose own
   table structure (column-header vocabulary + column count) most resembles
   PD's is preferred over an arbitrary "first one that parsed" pick.

3. **Semantic row-text scoring** (`semantic_similarity.py`, TZ 9.1's
   "семантические якоря"): each mechanism's `match_fn` returns, alongside
   its fingerprint, the matched row's own text (`anchor_search.
   row_text_at_word`). `pick_best_candidate` scores that text against the
   parameter's own `query_text` (name + catalog source hint, see
   `anchor_search.build_semantic_query`) and (a) prefers a higher-scoring
   candidate over fingerprinting alone when fingerprints tie or are absent,
   and (b) drops a candidate outright when its score falls below
   `semantic_similarity.SEMANTIC_MIN_SIMILARITY` -- this is the one place in
   this module that is a real *filter*, not just a re-ranking: it can turn a
   spurious keyword-tagger hit (e.g. checkpoint 40's "Стена в грунте" page
   for a building-area parameter) into an honest "nothing found" instead of
   a wrong value, even when it was the only candidate. Unavailable/disabled
   (no `query_text`, or the embedding model failed to load) is a no-op --
   candidates are scored `None` and never filtered on that basis alone.

4. **Existence/correctness signals** (`StageCandidate.is_empty_value` /
   `normative_mismatch`, optionally supplied by a mechanism's own `match_fn`
   -- see `MatchFn` below): real forensic finding, LOS3A PZ-001 ("Площадь
   застройки"): the live candidate tagger tagged BOTH the genuinely correct
   PD page AND an unrelated, unfilled certificate/template page whose own
   "found" number was a bare zero placeholder -- `pick_best_candidate`
   picked the latter simply because it ranked first by tagger confidence
   (there was no PD reference yet to fingerprint/semantic-score against).
   `is_empty_value` lets a mechanism flag exactly that ("I found A number,
   but it reads as an unfilled placeholder, not a measurement") as a ranking
   signal ranked ABOVE fingerprint/semantic scoring, not just alongside it --
   ties are still broken the old way once no candidate is flagged.
   `normative_mismatch` covers a distinct forensic finding (PZ-002, "Общая
   площадь здания"): a single document legitimately anchor-matches on two
   different rows referencing two different SP/GOST normative documents for
   two related-but-distinct quantities that share most of their vocabulary
   (too close for signal 3's embedding score to separate) -- see
   `anchor_search.extract_normative_references`/`expected_normative_
   references`. Both signals are opt-in per outcome (a `match_fn` may return
   a 3-, 4-, or 5-element tuple, see `MatchFn`) and default to "not flagged"
   when a mechanism does not compute them, so every pre-existing caller is
   completely unaffected until it explicitly opts in.

5. **Optional LLM verifier** (`llm_candidate_verifier.py`, OFF unless
   `CASE10_LLM_VERIFIER_ENABLED=1`): after `pick_best_candidate` has
   chosen, a compact instruct model is shown the same candidate pool (value,
   row, a few surrounding visual rows, document/page) plus the parameter's
   catalog definition and answers, as strict JSON, which candidate is that
   parameter or "none" -- it never produces a value itself. Never consulted
   when the deterministic ranking abstained. In the default "shadow" mode
   the verdict only rides along on the picked payload (`llm_verification`,
   surfaced in the evidence group's `delta`) for the inspector; "rerank"
   mode lets it replace the pick. See evaluation/LLM_CANDIDATE_VERIFIER_
   PILOT_REPORT.md for the measured pilot behind both defaults. With the flag off
   this signal is a strict no-op: nothing is computed, nothing changes.

Signals 1-4 are purely *additive* over the pre-existing "sort by
confidence, first successful page wins" behaviour, with two narrow,
deliberate exceptions: signal 3's filtering can turn an outcome that used to
be "wrong value accepted" into "no value", and signal 4's two flags can
demote (never outright drop) a candidate that used to win by confidence or
structural resemblance alone -- everything else about when a result exists
is otherwise unchanged from before this module existed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Iterable

from ..db.models import DocumentVersion, SourceFragment
from . import llm_candidate_verifier
from .anchor_search import TableFingerprint, fingerprint_similarity
from .dataset_sources import extract_original_pages
from .semantic_similarity import SEMANTIC_MIN_SIMILARITY, similarity as semantic_text_similarity

# Match-and-fingerprint callback each mechanism supplies: given a rendered
# page snapshot plus the (fragment, document, page, per-param extra payload)
# that produced it, return either None (no value found on this page) or a
# 3-, 4-, or 5-element tuple (payload, fingerprint_or_None, semantic_text_or_
# None[, is_empty_value[, normative_mismatch]]) -- `payload` is whatever
# object the mechanism itself wants back (its own Observation dataclass),
# `fingerprint` is that page's own `TableFingerprint` when the mechanism
# could build one, `semantic_text` is the matched row's own text (typically
# `anchor_search.row_text_at_word`'s return) used for the semantic
# re-ranking/filtering in `pick_best_candidate` below. The two trailing
# elements are optional (module docstring, signal 4) and default to `False`
# ("not flagged") when a mechanism's tuple omits them -- entirely opt-in,
# never required.
MatchFn = Callable[
    [dict[str, Any], SourceFragment, DocumentVersion, int, Any],
    "tuple[Any, ...] | None",
]

# One entry per (grouping key, stage) the caller wants resolved in this
# round: `key` groups results back together for the caller (a bare param_id
# for a PD round, or e.g. (param_id, stage) when RD and ID are resolved in
# the same round); `reference_document`/`reference_fingerprint` are the
# already-resolved PD stage's own document/fingerprint (or None when there
# is none -- see module docstring); `query_text` is the parameter's own
# semantic-query text (typically `anchor_search.build_semantic_query`'s
# return, or None to skip semantic scoring for this entry entirely).
StageEntry = tuple[Hashable, list[SourceFragment], DocumentVersion | None, TableFingerprint | None, Any, str | None]


def _confidence_key(fragment: SourceFragment) -> tuple[float, int, int]:
    return (-(fragment.confidence or 0), fragment.document_version_id, fragment.page or 0)


def narrow_candidate_fragments(
    fragments: Iterable[SourceFragment],
    by_id: dict[int, DocumentVersion],
    *,
    reference_document: DocumentVersion | None,
    limit: int,
) -> list[SourceFragment]:
    """Confidence-ranks `fragments`, then -- only when `reference_document`
    carries a `discipline` -- moves the ones whose own document shares it
    ahead of the rest (each half keeping its own confidence order), capped
    at `limit`. With no reference document/discipline, this is exactly the
    pre-existing `sorted(...)[:limit]` selection, unchanged."""
    ranked = sorted(fragments, key=_confidence_key)
    reference_discipline = getattr(reference_document, "discipline", None) if reference_document is not None else None
    if not reference_discipline:
        return ranked[:limit]
    narrow, wide = [], []
    for fragment in ranked:
        doc = by_id.get(fragment.document_version_id)
        if doc is not None and doc.discipline == reference_discipline:
            narrow.append(fragment)
        else:
            wide.append(fragment)
    if not narrow:
        return ranked[:limit]
    return (narrow + wide)[:limit]


@dataclass(slots=True)
class StageCandidate:
    fragment: SourceFragment
    document: DocumentVersion
    page: int
    payload: Any
    fingerprint: TableFingerprint | None
    # Cosine similarity of this candidate's own matched row text against the
    # parameter's `query_text` (see module docstring, signal 3) -- `None`
    # when no query_text/semantic_text was available or the embedding model
    # is disabled/unavailable, meaning "no signal", never "irrelevant".
    semantic_score: float | None = None
    # Module docstring, signal 4 -- both `False` ("not flagged") unless the
    # mechanism's own `match_fn` opted in by returning a 4-/5-element tuple.
    # `is_empty_value`: this candidate's own found value is a blank/zero
    # placeholder, not a genuine measurement (real finding: PZ-001, an
    # unfilled certificate template field). `normative_mismatch`: this
    # candidate's own nearby text cites a DIFFERENT SP/GOST normative
    # document than the parameter's own catalog-declared one (real finding:
    # PZ-002, a fire-safety document's "площадь помещений здания по СП
    # 118.13330.2012" row vs. the catalog's "Общая площадь здания" per СП
    # 54.13330.2016). Neither is a hard filter (see `pick_best_candidate`):
    # a flagged candidate can still win when it is the only one available,
    # exactly like the pre-existing "first match wins" fallback.
    is_empty_value: bool = False
    normative_mismatch: bool = False
    # Row/context text for the optional LLM verifier (module docstring,
    # signal 5) -- only populated when `CASE10_LLM_VERIFIER_ENABLED`.
    verifier_row_text: str | None = None
    verifier_context: str | None = None


def pick_best_candidate(
    candidates: list[StageCandidate], reference_fingerprint: TableFingerprint | None,
) -> StageCandidate | None:
    """First applies the semantic filter (module docstring, signal 3): any
    candidate with a `semantic_score` below `SEMANTIC_MIN_SIMILARITY` is
    dropped outright, even when it is the only candidate -- an honest "no
    value found" is preferred over a value pulled from a page whose own text
    reads nothing like the parameter being searched for. A candidate with no
    `semantic_score` at all (feature unavailable, or this mechanism/entry
    supplied no query_text) is never dropped by this step.

    Then applies the existence/correctness signals (module docstring, signal
    4): among the survivors, a candidate with neither `is_empty_value` nor
    `normative_mismatch` set is always preferred over one with either set,
    REGARDLESS of confidence order, fingerprint, or semantic score -- an
    empty placeholder or a candidate citing the wrong normative document is
    demoted, never treated as equal-quality evidence just because it parsed.
    This is strictly a demotion, not a filter: when every survivor is
    flagged (or there is only one survivor), the pre-existing behaviour
    below still picks among them exactly as if none were flagged.

    With no reference fingerprint (resolving PD itself, or PD was not
    found), returns the first unflagged survivor in original (narrowed,
    confidence-ranked) order when one exists; otherwise the first survivor
    lacking `is_empty_value` (a wrong-normative-reference candidate is still
    better than an outright empty one); otherwise the first survivor overall
    -- *exactly* the pre-existing "first match wins" behaviour when both
    flags are a no-op. With a reference, picks the highest
    `fingerprint_similarity` to it among the least-flagged survivors, ties
    broken by `semantic_score` (higher first), then by that same original
    order."""
    if not candidates:
        return None
    survivors = [c for c in candidates if c.semantic_score is None or c.semantic_score >= SEMANTIC_MIN_SIMILARITY]
    if not survivors:
        return None
    if reference_fingerprint is None:
        for candidate in survivors:
            if not candidate.is_empty_value and not candidate.normative_mismatch:
                return candidate
        for candidate in survivors:
            if not candidate.is_empty_value:
                return candidate
        return survivors[0]

    def score(index: int) -> tuple[int, int, float, float, int]:
        candidate = survivors[index]
        fingerprint_score = fingerprint_similarity(reference_fingerprint, candidate.fingerprint) if candidate.fingerprint is not None else -1.0
        semantic_score = candidate.semantic_score if candidate.semantic_score is not None else -1.0
        return (
            0 if not candidate.is_empty_value else -1,
            0 if not candidate.normative_mismatch else -1,
            fingerprint_score,
            semantic_score,
            -index,
        )

    best_index = max(range(len(survivors)), key=score)
    return survivors[best_index]


def resolve_stage_round(
    *,
    entries: list[StageEntry],
    by_id: dict[int, DocumentVersion],
    pages_per_stage: int,
    budget: dict[str, int],
    match_fn: MatchFn,
) -> dict[Hashable, StageCandidate]:
    """Renders up to `pages_per_stage` narrowed candidate pages per entry
    (batched across every entry sharing a document, exactly like the
    pre-existing per-mechanism `collect_*_observations` loops), then for
    each entry evaluates every rendered candidate that yields a value,
    scores it against that entry's own `query_text` (module docstring,
    signal 3) when both are available, and keeps the best one (see
    `pick_best_candidate`). Never renders more pages per entry than the
    pre-existing per-stage cap did."""
    needed_pages: dict[int, set[int]] = defaultdict(set)
    plan: list[tuple[Hashable, SourceFragment, DocumentVersion, int, Any]] = []
    for key, fragments, reference_document, _reference_fingerprint, extra, _query_text in entries:
        selected = narrow_candidate_fragments(fragments, by_id, reference_document=reference_document, limit=pages_per_stage)
        for fragment in selected:
            if budget["pages"] <= 0:
                break
            doc = by_id.get(fragment.document_version_id)
            if not doc or not fragment.page:
                continue
            page = int(fragment.page)
            if page not in needed_pages[doc.id]:
                needed_pages[doc.id].add(page)
                budget["pages"] -= 1
            plan.append((key, fragment, doc, page, extra))

    if not plan:
        return {}

    documents_by_id = {doc.id: doc for _key, _fragment, doc, _page, _extra in plan}
    snapshots: dict[tuple[int, int], dict[str, Any]] = {}
    for document_id, pages in needed_pages.items():
        document = documents_by_id.get(document_id) or by_id.get(document_id)
        if document is None:
            continue
        try:
            extracted = extract_original_pages(document, pages)
        except (FileNotFoundError, ValueError, RuntimeError):
            continue
        for page_number, snapshot in extracted.items():
            snapshots[(document_id, int(page_number))] = snapshot

    query_texts = {key: query_text for key, _f, _rd, _rfp, _extra, query_text in entries}
    verifier_enabled = llm_candidate_verifier.is_enabled()
    grouped: dict[Hashable, list[StageCandidate]] = defaultdict(list)
    for key, fragment, doc, page, extra in plan:
        snapshot = snapshots.get((doc.id, page))
        if not snapshot:
            continue
        outcome = match_fn(snapshot, fragment, doc, page, extra)
        if outcome is None:
            continue
        # `match_fn` may return 3, 4, or 5 elements -- the trailing
        # existence/correctness flags (module docstring, signal 4) are
        # opt-in per mechanism, so a mechanism that has not been updated to
        # compute them simply omits them and gets `False` ("not flagged")
        # for both, unchanged from before this signal existed.
        payload, fingerprint, semantic_text = outcome[0], outcome[1], outcome[2]
        is_empty_value = outcome[3] if len(outcome) > 3 else False
        normative_mismatch = outcome[4] if len(outcome) > 4 else False
        semantic_score = semantic_text_similarity(query_texts.get(key), semantic_text)
        verifier_row_text = verifier_context = None
        if verifier_enabled:
            verifier_row_text, verifier_context = llm_candidate_verifier.build_candidate_context(
                snapshot.get("words") or [], getattr(payload, "bbox_pdf", None),
            )
        grouped[key].append(StageCandidate(
            fragment=fragment, document=doc, page=page, payload=payload,
            fingerprint=fingerprint, semantic_score=semantic_score,
            is_empty_value=is_empty_value, normative_mismatch=normative_mismatch,
            verifier_row_text=verifier_row_text, verifier_context=verifier_context,
        ))

    reference_fingerprints = {key: reference_fingerprint for key, _f, _rd, reference_fingerprint, _extra, _qt in entries}
    extras = {key: extra for key, _f, _rd, _rfp, extra, _qt in entries}
    result: dict[Hashable, StageCandidate] = {}
    for key, candidates in grouped.items():
        best = pick_best_candidate(candidates, reference_fingerprints.get(key))
        if verifier_enabled:
            best, _verified = llm_candidate_verifier.review_pick(candidates, best, extras.get(key))
        if best is not None:
            result[key] = best
    return result
