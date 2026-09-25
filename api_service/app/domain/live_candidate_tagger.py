"""Live candidate tagger -- ТЗ 9.1 item 2, Phase A/B.

Every extraction mechanism in this pipeline (the 4 generic-tier mechanisms
via `official_evidence.inference_annotation()`, the 5 rule-pack codes via
`official_rule_packs.is_inference_locator()`) builds its candidate pool
*exclusively* from `SourceFragment` rows with `source_system ==
"learning_annotation"` -- rows the organizer's own `annotations.jsonl`
supplies for the 3 official hackathon objects only. No code anywhere in
this repo generated an equivalent row from a raw, newly-supplied document
(see the `project_case10_new_objects_no_tagger_gap` memory / `NEW_OBJECTS_
ANALYSE.md`): a real in-process run against 6 independent, structurally
rich real objects found exactly 0 `SourceFragment` rows and 0 comparable
`EvidenceGroup`s on every one of them, regardless of document richness.

This module is that missing layer. `tag_live_candidates` scans a document's
own pages (via the same rotation-corrected, OCR-capable snapshot pipeline
`dataset_sources.extract_original_pages` already gives every other
extractor) for each matrix parameter's own anchor phrase
(`anchor_search.find_anchor_end_index_for_phrase` -- literal, qualifier-
tolerant word-sequence matching; the module-level semantic fallback stays
off by default exactly as it already is for every other caller), then
writes a `SourceFragment` with a **new** `source_system == "live_tagger"`
in the same field shape `_upsert_source_fragment` writes for organizer
data, so it slots into the existing `by_code` candidate pools unmodified --
see the two one-line filter widenings in `official_evidence.py`/
`official_rule_packs.py`. This is purely additive: an object that already
has organizer tags keeps using them (this module runs alongside, not
instead of, them), and an object with none now gets a real candidate pool
instead of a structural zero.

**Phase B (semantic confirmation)**: before creating a fragment, the
matched row's own text (`anchor_search.row_text_at_word`) is scored against
the parameter's semantic query (`anchor_search.build_semantic_query`) via
`semantic_similarity.similarity`. A literal anchor-phrase match is still
just a word-sequence match -- it can land on a page that uses the same
words in an unrelated sense (the same class of false positive checkpoint 40
found for Novoslobodskaya's "Стена в грунте" page). A available-and-low
score drops the candidate before it is ever written; unavailable/no-signal
(`None`) never blocks a candidate, matching every other caller's contract
for this module.

**Deterministic by construction (plan -> scan -> merge)**. The output of a
run must be a pure function of the object's documents -- not of machine
speed, load, core count or scheduling (ТЗ reproducibility; the former
wall-clock budget scanned 65/103 LOS3A RD documents on a noisy machine and
all of them on a quiet one). So a run has three phases:

1. *plan* -- every candidate document gets a place in one TOTAL order
   (discipline-hint priority, then cheaper first, then `dataset_file_id`/
   hash/id as tie-breakers) and the documents/pages budget is spent down
   that order BEFORE any work starts. Which documents are scanned, and how
   many pages of each, is fixed here; nothing later can change it.
   The budget is a cap on the object's *total* scanned documents/pages
   (documents scanned by an earlier run are charged first), so a re-run
   converges to the same set instead of walking further down the list.
2. *scan* -- `live_tagger_scan.scan_document` per planned document: a pure
   function of (file bytes, page range, anchor list). Runs in a spawn-based
   process pool sized from the CPUs actually available (or in-process), and
   is served from an on-disk cache keyed by the file's SHA-256 when the same
   bytes were scanned before.
3. *merge* -- results are applied in PLAN ORDER by the parent, regardless of
   which worker finished first: semantic confirmation, `SourceFragment`
   writes (so row ids are assigned in a fixed order), coverage markers.

Parallelism and the cache therefore only change how fast the same answer is
reached, never which answer.

**Coverage is explicit**. Every document carries a `live_tagger_scan` marker
(status/pages) in its own `dataset_metadata` and `live_tagger_coverage`
turns those into per-stage "scanned N of M documents" numbers that
`v3_pipeline.build_protocol_payload` prints into the protocol -- a document
the caps left out is visible as `not_scanned`, never silently absent.

**Idempotent per document**: a document already carrying a completed scan
marker is skipped on a later call (`force=True` overrides), so the scan cost
is paid once per document, not once per pipeline run -- it does not compete
with the ТЗ 11 "<=2 minutes per 132-parameter comparison" budget for any run
after the first.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
import logging
import multiprocessing
import time
from typing import Any, Iterable

from ..config import settings
from ..db.models import DocumentVersion, Param, SourceFragment
from . import dataset_sources
from .anchor_search import build_semantic_query, source_hints
from .anchor_vocab import anchor_phrases
from .dataset_sources import document_original_ref, document_page_count_hint, extract_original_pages
from .live_tagger_scan import (
    available_cpu_count,
    cache_key,
    init_worker,
    load_cached_scan,
    run_scan_task,
    scan_document,
    store_cached_scan,
)
from .matrix_unit_classifier import normalize_anchor_text
from .semantic_similarity import SEMANTIC_MIN_SIMILARITY, similarity as semantic_text_similarity

logger = logging.getLogger(__name__)

LIVE_TAGGER_SOURCE_SYSTEM = "live_tagger"
LIVE_TAGGER_EXTRACTOR = "live_anchor_tagger_v1"
_STAGES = ("PD", "RD", "ID")
_STAGE_SOURCE_FIELD = {"PD": "source_pd", "RD": "source_rd", "ID": "source_id"}
_BASE_CONFIDENCE = 0.5
_MIN_ANCHOR_CHARS = 4

# Per-document scan marker in `dataset_metadata`. Deliberately carries NO
# timestamp: a wall-clock value in stored state is exactly the kind of
# run-to-run difference this module exists to remove. The pre-determinism
# marker (`live_tagger_scanned_at`, an ISO timestamp) is still honoured on
# read so existing databases are not needlessly re-scanned.
SCAN_MARKER_KEY = "live_tagger_scan"
_LEGACY_MARKER_KEY = "live_tagger_scanned_at"
_DONE_STATUSES = frozenset({"scanned", "partial"})

# What "the real thing" looks like, captured at import: if either has been
# replaced in-process (an evaluation harness or test monkeypatching the byte
# source), worker processes would not see the replacement and would scan
# different data -- so such a run stays in-process.
_REAL_ORIGINAL_BYTES = dataset_sources.original_document_bytes
_REAL_EXTRACT_PAGES = extract_original_pages


def is_live_tagger_enabled() -> bool:
    return bool(settings.LIVE_TAGGER_ENABLED)


def new_live_tagger_budget() -> dict[str, float]:
    """One shared, mutable budget for a whole `tag_live_candidates` call:
    documents and pages only -- see config.py's `LIVE_TAGGER_MAX_*` for how
    these deterministic caps are sized (and why wall-clock is not one)."""
    return {
        "documents": float(settings.LIVE_TAGGER_MAX_DOCUMENTS),
        "pages": float(settings.LIVE_TAGGER_MAX_PAGES_TOTAL),
    }


@dataclass(slots=True)
class _ParamAnchor:
    param: Param
    code: str
    anchor_phrase: str
    query_text: str
    hint_disciplines: frozenset[str]


@dataclass(slots=True)
class _PlanItem:
    """One document the plan phase decided to scan."""
    document: DocumentVersion
    stage: str
    anchors: list[_ParamAnchor]
    pairs: list[tuple[str, str]]
    relative: str
    sha256: str
    pages_total: int
    pages: list[int]
    key: str = ""
    result: dict[str, Any] | None = field(default=None)


def _document_stage(document: DocumentVersion) -> str | None:
    """Exactly the same literal check `generic_matrix_extraction._bucket_
    by_stage` applies to a document's own `dataset_stage` -- RD_ID_MIXED and
    any other non-PD/RD/ID value are excluded, not remapped, because the
    generic-tier candidate pools this module feeds never accept them
    either."""
    raw = str(document.dataset_stage or "").upper()
    return raw if raw in _STAGES else None


def _canonical_discipline(value: str | None) -> str | None:
    if not value:
        return None
    from evaluation.candidate_coverage import DISCIPLINE_ALIASES

    upper = str(value).strip().upper()
    return DISCIPLINE_ALIASES.get(upper, upper) or None


def _build_param_anchors(params: list[Param]) -> dict[str, list[_ParamAnchor]]:
    """{stage: [_ParamAnchor, ...]} for every param with a distinctive
    enough name to search for at all -- mirrors `matrix_unit_classifier.
    _has_distinctive_anchor_name`'s bar loosely (a bare number/short code is
    not a safe literal anchor), but is intentionally NOT restricted to
    NUMERIC_SIMPLE/eligible-for-the-generic-extractor params the way
    `generic_matrix_extraction.eligible_params` is: this module only
    *locates candidate pages*, it never parses a value itself, so it is
    useful for rule-pack codes and enum/compound/count params too -- every
    downstream mechanism re-derives its own value independently once a page
    is rendered (see module docstring).

    Anchors are ordered by parameter code, not by whatever order the caller's
    (unordered) catalog query returned them in: the anchor order fixes the
    order fragments are written in, which fixes their row ids."""
    from evaluation.candidate_coverage import parse_discipline_hints

    by_stage: dict[str, list[_ParamAnchor]] = {stage: [] for stage in _STAGES}
    for param in sorted(params, key=lambda p: str(p.code)):
        name = str(param.parameter_name or "").strip()
        hints = None
        for stage in _STAGES:
            if not getattr(param, _STAGE_SOURCE_FIELD[stage], False):
                continue
            # The catalog name first, then the anchor vocabulary's wordings for this stage (Phase 12, P0 hook): with
            # empty vocabulary files this is exactly [name], so anchors, scan-cache keys and output are unchanged.
            phrases = [phrase for phrase in anchor_phrases(str(param.code), name, stage)
                       if len(normalize_anchor_text(phrase)) >= _MIN_ANCHOR_CHARS]
            if not phrases:
                continue
            hints = hints if hints is not None else source_hints(param)
            query_text = build_semantic_query(param, stage)
            hint_disciplines = frozenset(parse_discipline_hints(hints.get(stage)))
            for phrase in phrases:
                by_stage[stage].append(_ParamAnchor(
                    param=param,
                    code=str(param.code),
                    anchor_phrase=phrase,
                    query_text=query_text,
                    hint_disciplines=hint_disciplines,
                ))
    return by_stage


def _applicable_anchors(anchors: list[_ParamAnchor], discipline: str | None) -> list[_ParamAnchor]:
    """A param with a parsed discipline hint only applies to a document
    whose own (canonicalized) discipline is in that hint set; a param with
    no parsed hint applies to any document at the stage (same "unknown hint
    is never treated as impossible" rule `evaluation.candidate_coverage.
    coverage_report` already uses for the structural upper-bound estimate
    this module turns into a real scan)."""
    if discipline is None:
        return [a for a in anchors if not a.hint_disciplines]
    return [a for a in anchors if not a.hint_disciplines or discipline in a.hint_disciplines]


# ---------------------------------------------------------------- markers


def scan_marker(document: DocumentVersion) -> dict[str, Any] | None:
    """The document's scan marker, or `None` if it was never looked at.
    `status` is one of scanned | partial | failed | not_applicable (a legacy
    timestamp-only marker reads as `scanned`)."""
    meta = document.dataset_metadata or {}
    marker = meta.get(SCAN_MARKER_KEY)
    if isinstance(marker, dict):
        return marker
    if meta.get(_LEGACY_MARKER_KEY):
        return {"status": "scanned", "legacy": True}
    return None


def _already_scanned(document: DocumentVersion) -> bool:
    marker = scan_marker(document)
    return bool(marker) and marker.get("status") in _DONE_STATUSES


def _set_marker(document: DocumentVersion, **fields: Any) -> None:
    metadata = dict(document.dataset_metadata or {})
    metadata[SCAN_MARKER_KEY] = fields
    document.dataset_metadata = metadata


# ---------------------------------------------------------------- plan


def _document_sort_key(document: DocumentVersion) -> tuple[str, str, int]:
    """Total, content-derived order over documents. `id` alone would tie the
    result to insertion order; `dataset_file_id`/hash come first because they
    are properties of the data, `id` only breaks a remaining tie."""
    return (
        str(getattr(document, "dataset_file_id", None) or ""),
        str(document.file_hash or document.content_hash or ""),
        int(document.id or 0),
    )


def _document_priority(document: DocumentVersion, anchors: list[_ParamAnchor]) -> tuple[int, int, int]:
    discipline = _canonical_discipline(document.discipline)
    confident = sum(1 for a in anchors if a.hint_disciplines and discipline in a.hint_disciplines)
    applicable = len(_applicable_anchors(anchors, discipline))
    page_count = document_page_count_hint(document)
    # Most confident-discipline-match params first, then more applicable
    # params, then cheaper (fewer-page) documents -- maximizes how many
    # genuinely-relevant documents a bounded budget actually covers.
    return (-confident, -applicable, page_count)


def _confidence(semantic_score: float | None) -> float:
    if semantic_score is None:
        return _BASE_CONFIDENCE
    return round(min(0.9, _BASE_CONFIDENCE + max(0.0, semantic_score - SEMANTIC_MIN_SIMILARITY)), 3)


def _scan_settings() -> dict[str, Any]:
    """Settings that change what a scan of identical bytes returns -- part of
    every cache key."""
    from .semantic_similarity import is_anchor_fallback_enabled

    return {
        "ocr_engine": settings.OCR_ENGINE,
        "ocr_lang": settings.OCR_LANG,
        "ocr_paddle_lang": getattr(settings, "OCR_PADDLE_LANG", None),
        "semantic_anchor_fallback": is_anchor_fallback_enabled(),
    }


def _plan_scans(
    docs: list[DocumentVersion],
    by_stage: dict[str, list[_ParamAnchor]],
    budget: dict[str, float],
    force: bool,
    diagnostics: dict[str, Any],
) -> list[_PlanItem]:
    """Decide, deterministically and before any page is opened, which
    documents are scanned and how many pages of each (see module docstring)."""
    per_document_cap = max(0, int(settings.LIVE_TAGGER_MAX_PAGES_PER_DOCUMENT))
    pairs_by_stage = {stage: [(a.code, a.anchor_phrase) for a in anchors] for stage, anchors in by_stage.items()}

    candidates: list[tuple[tuple[int, int, int], tuple[str, str, int], DocumentVersion, str]] = []
    for document in sorted(docs, key=_document_sort_key):
        stage = _document_stage(document)
        if stage is None or not by_stage.get(stage):
            continue
        if document_original_ref(document) is None:
            # No readable PDF original (docx/rar/... or no manifest path): it
            # can never yield a page, so it takes no budget and no scan.
            # Reported as `not_scannable` by `live_tagger_coverage`.
            diagnostics["documents_not_scannable"] += 1
            continue
        if not force and _already_scanned(document):
            diagnostics["documents_skipped_already_tagged"] += 1
            # Charged against this run's caps: they bound the object's TOTAL
            # scanned documents/pages, so a re-run cannot walk past them.
            marker = scan_marker(document) or {}
            recorded_pages = marker.get("pages_scanned")
            budget["documents"] -= 1
            budget["pages"] -= float(recorded_pages if isinstance(recorded_pages, int) else min(document_page_count_hint(document), per_document_cap))
            continue
        discipline = _canonical_discipline(document.discipline)
        applicable = _applicable_anchors(by_stage[stage], discipline)
        if not applicable:
            # Not even a discipline-agnostic (hint-less) param wants this
            # document -- genuinely not worth opening.
            diagnostics["documents_not_applicable"] += 1
            _set_marker(document, status="not_applicable")
            continue
        candidates.append((_document_priority(document, applicable), _document_sort_key(document), document, stage))

    candidates.sort(key=lambda item: (item[0], item[1]))

    plan: list[_PlanItem] = []
    for _priority, _tiebreak, document, stage in candidates:
        if budget["documents"] <= 0 or budget["pages"] <= 0:
            # Cap reached: this and every later (lower-priority) document is
            # left UNMARKED -- visible as `not_scanned` in the coverage report
            # and eligible on a later run with a bigger cap.
            diagnostics["documents_deferred_by_cap"] += 1
            continue
        relative, sha256 = document_original_ref(document)  # type: ignore[misc]  # non-None: checked above
        pages_total = document_page_count_hint(document)
        page_count = min(pages_total, per_document_cap, int(budget["pages"]))
        if page_count <= 0:
            diagnostics["documents_deferred_by_cap"] += 1
            continue
        budget["documents"] -= 1
        budget["pages"] -= page_count
        anchors = by_stage[stage]
        pairs = pairs_by_stage[stage]
        plan.append(_PlanItem(
            document=document, stage=stage, anchors=anchors, pairs=pairs, relative=relative,
            sha256=sha256, pages_total=pages_total, pages=list(range(1, page_count + 1)),
        ))
    diagnostics["documents_planned"] = len(plan)
    return plan


# ---------------------------------------------------------------- scan


def _page_source_replaced() -> bool:
    """True when the byte/page source has been replaced in-process (an
    evaluation harness or a test monkeypatching it). Such a run reads data
    that is NOT a function of the document's SHA-256, so it must neither use
    worker processes (they would not see the replacement) nor touch the
    SHA-256-keyed result cache (it would store fixture data under a real
    file's key, or serve a real file's cached hits instead of the fixture)."""
    return (
        dataset_sources.original_document_bytes is not _REAL_ORIGINAL_BYTES
        or extract_original_pages is not _REAL_EXTRACT_PAGES
    )


def _in_process_required() -> bool:
    """Runs that must not use worker processes: a replaced page source (see
    above), and the optional semantic anchor fallback, which needs the (large)
    embedding model in the scanning process."""
    from .semantic_similarity import is_anchor_fallback_enabled

    return is_anchor_fallback_enabled() or _page_source_replaced()


def _worker_count(pending: list[_PlanItem]) -> int:
    if not pending or _in_process_required():
        return 1
    if multiprocessing.parent_process() is not None:
        # Already inside a worker process (e.g. an entry-point script without an
        # `if __name__ == "__main__":` guard being re-imported by a spawned
        # child): never nest pools.
        logger.warning("live_tagger: called inside a worker process; scanning in-process (is the entry point missing a __main__ guard?)")
        return 1
    if settings.LIVE_TAGGER_WORKERS > 0:
        workers = settings.LIVE_TAGGER_WORKERS
    else:
        workers = min(available_cpu_count(), max(1, settings.LIVE_TAGGER_MAX_WORKERS))
        total_pages = sum(len(item.pages) for item in pending)
        workers = min(workers, max(1, total_pages // max(1, settings.LIVE_TAGGER_MIN_PAGES_PER_WORKER)))
    return max(1, min(workers, len(pending)))


def _scan_in_process(item: _PlanItem) -> dict[str, Any]:
    cpu_started = time.process_time()
    result = _scan_in_process_inner(item)
    result["_cpu_seconds"] = time.process_time() - cpu_started
    return result


def _scan_in_process_inner(item: _PlanItem) -> dict[str, Any]:
    try:
        # Late-bound lookup of `extract_original_pages` so a test/harness that
        # replaces it on this module is honoured.
        return scan_document(item.document, item.pages, item.pairs, lambda doc, pages: extract_original_pages(doc, pages))
    except Exception as exc:  # noqa: BLE001 -- one unreadable PDF must not sink the object's run
        logger.warning("live_tagger: scan of %s failed: %s: %s", getattr(item.document, "dataset_file_id", None) or item.document.id, type(exc).__name__, exc)
        return {"failed": type(exc).__name__, "detail": str(exc)[:200]}


def _run_pool(
    pending: list[_PlanItem], workers: int, results: dict[str, dict[str, Any]], cache_dir: Any, diagnostics: dict[str, Any],
) -> tuple[list[_PlanItem], int]:
    """Scans `pending` on a spawn process pool, filling `results` by cache
    key as workers finish (in completion order -- the merge phase re-imposes
    plan order). Returns `(leftover, worker_failures)`: the items that still
    have no usable result -- any a worker reported as failed (retried once
    in-process by the caller) and everything unfinished if the pool itself
    broke -- and how many documents the WORKERS failed on (for the log/
    diagnostics: a pool whose workers fail everything is a misconfiguration,
    not a bad PDF)."""
    remaining = {item.key: item for item in pending}
    worker_failures: dict[str, str] = {}
    # Longest-processing-time-first submission balances the pool (one 220-page
    # drawing set no longer starts last); it only affects wall time.
    order = sorted(pending, key=lambda item: (-len(item.pages), item.key))
    started = time.monotonic()
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"), initializer=init_worker) as pool:
            futures = {
                pool.submit(run_scan_task, {"relative": item.relative, "sha256": item.sha256, "pages": item.pages, "anchors": item.pairs}): item
                for item in order
            }
            finished = 0
            next_report = max(1, len(futures) // 10)
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                except BrokenProcessPool:
                    raise
                except Exception as exc:  # noqa: BLE001
                    result = {"failed": type(exc).__name__, "detail": str(exc)[:200]}
                finished += 1
                diagnostics["scan_cpu_seconds"] += float(result.pop("_cpu_seconds", 0.0))
                if "failed" not in result:
                    results[item.key] = result
                    remaining.pop(item.key, None)
                    if cache_dir is not None:
                        store_cached_scan(cache_dir, item.sha256, item.key, result)
                else:
                    worker_failures[item.key] = str(result.get("failed"))
                if finished >= next_report:
                    logger.info("live_tagger: %d/%d documents scanned (%.0fs)", finished, len(futures), time.monotonic() - started)
                    next_report += max(1, len(futures) // 10)
    except (BrokenProcessPool, OSError, RuntimeError, ImportError) as exc:
        logger.warning("live_tagger: worker pool failed (%s: %s); finishing %d documents in-process", type(exc).__name__, exc, len(remaining))
    if worker_failures:
        logger.warning(
            "live_tagger: workers failed on %d document(s) (%s); retrying them in-process",
            len(worker_failures), ", ".join(sorted(set(worker_failures.values()))),
        )
    return list(remaining.values()), len(worker_failures)


def _execute_scans(plan: list[_PlanItem], diagnostics: dict[str, Any]) -> None:
    """Fills `item.result` for every planned item: from the on-disk cache when
    the same bytes were scanned before, else by scanning (pool or in-process).
    Two planned documents with identical content+settings share one scan."""
    cache_dir = settings.LIVE_TAGGER_CACHE_DIR if settings.LIVE_TAGGER_CACHE_ENABLED and not _page_source_replaced() else None
    extra = _scan_settings()
    for item in plan:
        item.key = cache_key(item.sha256, item.stage, item.pairs, len(item.pages), extra)

    results: dict[str, dict[str, Any]] = {}
    pending: dict[str, _PlanItem] = {}
    looked_up: set[str] = set()
    for item in plan:
        if item.key not in looked_up:
            looked_up.add(item.key)
            cached = load_cached_scan(cache_dir, item.sha256, item.key) if cache_dir is not None else None
            if cached is not None:
                results[item.key] = cached
            else:
                pending[item.key] = item
        if item.key in results:
            diagnostics["cache_hits"] += 1

    todo = list(pending.values())
    workers = _worker_count(todo)
    diagnostics["workers"] = workers if todo else 0
    leftover = todo
    if todo and workers > 1:
        leftover, diagnostics["worker_failures_retried"] = _run_pool(todo, workers, results, cache_dir, diagnostics)
    for item in leftover:
        result = _scan_in_process(item)
        diagnostics["scan_cpu_seconds"] += float(result.pop("_cpu_seconds", 0.0))
        results[item.key] = result
        if cache_dir is not None:
            store_cached_scan(cache_dir, item.sha256, item.key, result)

    for item in plan:
        item.result = results[item.key]


# ---------------------------------------------------------------- merge


def _existing_live_fragments(db: Any, document_ids: list[int]) -> dict[tuple[int, str], SourceFragment]:
    """Preloads this call's own prior `live_tagger` rows so a `force=True`
    re-scan (or two hits landing on the same (document, page, code) within
    one call) updates the existing row in place instead of colliding with
    the `(document_version_id, source_system, external_id)` uniqueness
    constraint -- same upsert shape `official_dataset._upsert_source_
    fragment` already uses for organizer data."""
    if not document_ids:
        return {}
    rows = (
        db.query(SourceFragment)
        .filter(
            SourceFragment.document_version_id.in_(document_ids),
            SourceFragment.source_system == LIVE_TAGGER_SOURCE_SYSTEM,
        )
        .all()
    )
    return {(int(row.document_version_id), row.external_id): row for row in rows}


def _merge_item(db: Any, item: _PlanItem, diagnostics: dict[str, Any], existing: dict[tuple[int, str], SourceFragment]) -> None:
    document = item.document
    result = item.result or {}
    if "failed" in result:
        diagnostics["documents_failed"] += 1
        _set_marker(document, status="failed", error=str(result.get("failed")))
        db.add(document)
        return
    diagnostics["documents_scanned"] += 1
    diagnostics["pages_scanned"] += int(result.get("pages_scanned") or 0)
    anchor_by_code = {a.code: a for a in item.anchors}
    for hit in result.get("hits") or []:
        anchor = anchor_by_code.get(hit["code"])
        if anchor is None:
            continue
        row_text = hit.get("row_text")
        semantic_score = semantic_text_similarity(anchor.query_text, row_text)
        if semantic_score is None:
            diagnostics["semantic_unavailable"] += 1
        else:
            diagnostics["semantic_scored"] += 1
            if semantic_score < SEMANTIC_MIN_SIMILARITY:
                diagnostics["semantic_rejections"] += 1
                continue
        page_number = int(hit["page"])
        external_id = f"LIVE:{page_number}:{anchor.code}"
        key = (int(document.id), external_id)
        fragment = existing.get(key)
        is_new = fragment is None
        if fragment is None:
            fragment = SourceFragment(
                document_version_id=int(document.id),
                source_system=LIVE_TAGGER_SOURCE_SYSTEM,
                external_id=external_id,
            )
            existing[key] = fragment
        fragment.page = page_number
        fragment.bbox_pdf = hit.get("bbox_pdf")
        fragment.page_width = hit.get("width")
        fragment.page_height = hit.get("height")
        fragment.text = (row_text or anchor.anchor_phrase)[:500]
        fragment.fragment_type = "matrix_field"
        fragment.extractor = LIVE_TAGGER_EXTRACTOR
        fragment.confidence = _confidence(semantic_score)
        fragment.metadata_json = {
            "code": anchor.code,
            "annotation_type": "MATRIX_FIELD",
            "status": "AUTO_FIELD_CANDIDATE",
            "source": "LIVE_TAGGER_ANCHOR",
            "check_id": None,
            "stage": item.stage,
            "semantic_score": semantic_score,
        }
        db.add(fragment)
        diagnostics["fragments_created" if is_new else "fragments_updated"] += 1
    _set_marker(
        document,
        status="partial" if len(item.pages) < item.pages_total else "scanned",
        pages_scanned=int(result.get("pages_scanned") or 0),
        pages_total=int(item.pages_total),
        pages_no_text=int(result.get("pages_no_text") or 0),
    )
    db.add(document)


def tag_live_candidates(
    db: Any,
    docs: list[DocumentVersion],
    params: list[Param],
    *,
    budget: dict[str, float] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Scans `docs` for `params`' own anchor phrases and writes `live_tagger`
    `SourceFragment` rows (see module docstring). Returns scan diagnostics
    (`coverage` = per-stage scanned-of-total, see `live_tagger_coverage`);
    never raises for a single bad document (a page-render failure just
    records that document as failed, mirroring `resolve_stage_round`/
    `extract_official_rule_observations`'s existing error handling)."""
    diagnostics: dict[str, Any] = {
        "documents_scanned": 0, "documents_skipped_already_tagged": 0, "documents_failed": 0,
        "pages_scanned": 0, "fragments_created": 0, "fragments_updated": 0, "semantic_rejections": 0,
        "semantic_scored": 0, "semantic_unavailable": 0,
        "documents_planned": 0, "documents_deferred_by_cap": 0, "documents_not_scannable": 0,
        "documents_not_applicable": 0, "cache_hits": 0, "workers": 0, "worker_failures_retried": 0,
        "scan_cpu_seconds": 0.0,
    }
    if not is_live_tagger_enabled() or not docs or not params:
        return diagnostics
    budget = budget if budget is not None else new_live_tagger_budget()
    by_stage = _build_param_anchors(params)
    if not any(by_stage.values()):
        return diagnostics

    started = time.monotonic()
    # `applicable` (discipline-narrowed) only ever decides (a) whether a
    # document is worth opening at all and (b) scan ORDER under budget
    # pressure -- never which anchors are searched once it IS opened. Real
    # forensic counter-example (LOS3A RD page 11, see the live-candidate-
    # tagger checkpoint memory): PZ-001's own catalog hint ("Раздел ПП (ГП):
    # ...") parses to discipline ГП, but the real ТЭП table restating it
    # sits on an АР (architecture) "Общие данные" sheet alongside PZ-002/
    # PZ-004/etc, whose hints DO match АР (or have none) -- a hard
    # discipline filter on the SEARCH set would exclude a genuine match the
    # moment any OTHER param's hint got the document opened at all. Per-page
    # anchor-search cost across the whole 132-code catalog is not the
    # bottleneck (measured: negligible next to the page-render/text-
    # extraction cost that already happens once a document is selected --
    # see config.py's LIVE_TAGGER_* comment), so there is no budget reason
    # to narrow the search itself; this mirrors the role `cross_stage_
    # localization.narrow_candidate_fragments` already gives a discipline
    # hint elsewhere in this codebase (rank/prefer, never exclude).
    plan = _plan_scans(docs, by_stage, budget, force, diagnostics)
    planned_at = time.monotonic()
    scanned_at = planned_at
    if plan:
        _execute_scans(plan, diagnostics)
        scanned_at = time.monotonic()
        existing = _existing_live_fragments(db, [int(item.document.id) for item in plan])
        # Plan order, not completion order: fragment rows get their ids in a
        # fixed sequence whatever the worker scheduling was.
        for item in plan:
            _merge_item(db, item, diagnostics, existing)
    db.flush()
    diagnostics["coverage"] = live_tagger_coverage(docs)
    finished = time.monotonic()
    # Timings are for logs/measurement only -- never persisted, never part of
    # any output that must be reproducible.
    diagnostics["scan_cpu_seconds"] = round(diagnostics["scan_cpu_seconds"], 2)
    diagnostics["plan_seconds"] = round(planned_at - started, 2)
    diagnostics["scan_seconds"] = round(scanned_at - planned_at, 2)
    diagnostics["merge_seconds"] = round(finished - scanned_at, 2)
    diagnostics["elapsed_seconds"] = round(finished - started, 2)
    logger.info(
        "live_tagger: planned=%d scanned=%d failed=%d deferred_by_cap=%d cache_hits=%d workers=%d pages=%d fragments=%d in %.1fs",
        diagnostics["documents_planned"], diagnostics["documents_scanned"], diagnostics["documents_failed"],
        diagnostics["documents_deferred_by_cap"], diagnostics["cache_hits"], diagnostics["workers"],
        diagnostics["pages_scanned"], diagnostics["fragments_created"] + diagnostics["fragments_updated"],
        diagnostics["elapsed_seconds"],
    )
    return diagnostics


# ---------------------------------------------------------------- coverage


def live_tagger_coverage(docs: Iterable[DocumentVersion]) -> dict[str, Any]:
    """Per-stage "scanned N of M documents", derived ONLY from the documents'
    own scan markers (no clock, no run history), so it is identical for a
    fresh run, a cached run and a re-run, and can be recomputed at any time --
    it is what `build_protocol_payload` prints into the protocol.

    `documents_scannable` (M) = documents of the stage that have a PDF original
    and to which at least one catalog parameter applies; the rest are counted
    separately (`not_scannable`: docx/rar/...; `not_applicable`: no parameter
    wants this stage/discipline) instead of silently shrinking the total.
    `not_scanned` = scannable but never scanned (deterministic cap reached, or
    the tagger is disabled); `partial` = scanned with only the first pages
    (per-document page cap); `no_text_layer` = scanned but not one page had a
    text layer (image-only PDFs: the tagger deliberately does not OCR)."""
    stages: dict[str, dict[str, int]] = {
        stage: {
            "documents_total": 0, "documents_scannable": 0, "documents_scanned": 0, "documents_partial": 0,
            "documents_no_text_layer": 0, "documents_failed": 0, "documents_not_scanned": 0,
            "documents_not_scannable": 0, "documents_not_applicable": 0, "pages_scanned": 0, "pages_total": 0,
        }
        for stage in _STAGES
    }
    other_stage = 0
    for document in docs:
        stage = _document_stage(document)
        if stage is None:
            other_stage += 1
            continue
        row = stages[stage]
        row["documents_total"] += 1
        marker = scan_marker(document) or {}
        status = marker.get("status")
        if status in _DONE_STATUSES:
            row["documents_scannable"] += 1
            row["documents_scanned"] += 1
            if status == "partial":
                row["documents_partial"] += 1
            scanned_pages = marker.get("pages_scanned")
            if isinstance(scanned_pages, int):
                row["pages_scanned"] += scanned_pages
                row["pages_total"] += int(marker.get("pages_total") or scanned_pages)
                if scanned_pages > 0 and int(marker.get("pages_no_text") or 0) >= scanned_pages:
                    row["documents_no_text_layer"] += 1
        elif document_original_ref(document) is None:
            row["documents_not_scannable"] += 1
        elif status == "not_applicable":
            row["documents_not_applicable"] += 1
        else:
            row["documents_scannable"] += 1
            row["documents_failed" if status == "failed" else "documents_not_scanned"] += 1

    enabled = is_live_tagger_enabled()
    lines = []
    for stage in _STAGES:
        row = stages[stage]
        if not row["documents_total"]:
            continue
        line = f"{stage}: просканировано {row['documents_scanned']} из {row['documents_scannable']} документов"
        notes = []
        if row["documents_partial"]:
            notes.append(f"только первые страницы у {row['documents_partial']}")
        if row["documents_no_text_layer"]:
            notes.append(f"без текстового слоя {row['documents_no_text_layer']}")
        if row["documents_failed"]:
            notes.append(f"ошибок чтения {row['documents_failed']}")
        if row["documents_not_scanned"]:
            notes.append(f"не просканировано из-за лимита {row['documents_not_scanned']}")
        if row["documents_not_scannable"]:
            notes.append(f"не PDF {row['documents_not_scannable']}")
        if row["documents_not_applicable"]:
            notes.append(f"не применимо {row['documents_not_applicable']}")
        lines.append(line + (f" ({'; '.join(notes)})" if notes else ""))
    complete = enabled and all(
        row["documents_not_scanned"] == 0 and row["documents_failed"] == 0 for row in stages.values()
    )
    return {
        "enabled": enabled,
        "complete": complete,
        "stages": stages,
        "documents_other_stage": other_stage,
        "summary": "Живой тэггер кандидатов: " + ("; ".join(lines) if lines else "нет документов стадий ПД/РД/ИД")
        if enabled else "Живой тэггер кандидатов отключён",
    }
