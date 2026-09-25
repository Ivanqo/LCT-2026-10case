"""Generic (data-driven, not per-code) table-row-count extraction.

Fourth sibling of the anchor family (`generic_matrix_extraction.py`,
`generic_enum_extraction.py`, `generic_compound_extraction.py`), for the
NUMERIC_COUNT parameters where the real quantity lives as one row per
physical item in a real specification/exposition/schedule table (apartments,
parking spaces, handrails, ...) rather than as a single labelled scalar. The
anchor here is not the parameter's own catalog name (which rarely appears
verbatim as a page's table title) but a title phrase derived from the
parameter's own `source_pd`/`source_rd`/`source_id` catalog text -- see
`anchor_search.derive_title_candidates`.

Row structure is recovered by Y/X-clustering the page's own word bboxes
(`_cluster_rows`), not PyMuPDF's `page.find_tables()`: the snapshot contract
this whole extractor family already runs on (`dataset_sources.
extract_original_pages`) intentionally exposes only a flat word list, never
the raw `fitz.Page` object, to keep every extractor here independent of the
PDF library's own version-specific table-detection API -- widening that
contract for one extractor was judged a bigger structural change than this
session's single-iteration budget for the fallback justifies (see
CASE10_MATRIX_132_COVERAGE.md). The clustering fallback is intentionally
simple (fixed Y-tolerance banding, a small header-marker vocabulary, a small
table-end vocabulary) and its limitations are documented there rather than
iterated on further.

Only ever eligible for the explicit semantic allowlist in
`matrix_unit_classifier.TABLE_ROW_COUNT_SEMANTIC_CODES` -- unit alone
(`NUMERIC_COUNT`) cannot distinguish "count these rows" from "take the
peak/duration/threshold reading off this table" (see that module for the
full reasoning); applying row-counting to the wrong parameter would not fail
safely, so this dimension is a maintained list, not a keyword heuristic.

Like its siblings, conservative by construction: every result lands on
`CANDIDATE` (never an auto-confirmed violation) pending inspector review.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from ..db.models import DocumentVersion, Param, SourceFragment
from .anchor_search import (
    GENERIC_SITE_LOCATION,
    TableFingerprint,
    build_semantic_query,
    derive_title_candidates,
    find_anchor_end_index_for_phrase,
    row_text_at_word,
    source_hints,
    table_fingerprint_from_row,
    union_bbox,
)
from .cross_stage_localization import resolve_stage_round
from .matrix_unit_classifier import is_table_count_eligible

GENERIC_TABLE_COUNT_EXTRACTOR_VERSION = "generic-table-row-count-v1"
GENERIC_TABLE_COUNT_EXTRACTOR_NAME = "generic_table_row_count"

PAGES_PER_STAGE = 15
DEFAULT_PAGE_BUDGET = 1200

# Row-clustering tuning. Two word bboxes are the "same row" when their
# vertical centers are within this many PDF points of the row's own first
# word -- typical single-line text leading is 10-14pt, so this comfortably
# bands one text line without merging two adjacent ones.
_ROW_Y_TOLERANCE = 3.5
# How far below the anchor (as a fraction of page height) rows are still
# worth considering part of the SAME table -- bounds the scan to roughly the
# rest of the sheet without crossing into an unrelated title block by pure
# row-count budget alone.
_MAX_TABLE_HEIGHT_FRACTION = 0.7
_MAX_ROWS_SCANNED = 80
# A row needs at least this many word tokens to count as a real table row
# (excludes stray single-word footer/page-number noise below the table).
_MIN_ROW_WORDS = 2
# Leading rows matching these markers are the table's own header (column
# labels), never counted as content -- checked against a row's own word set,
# case/ё-insensitive (see `_normalize_row_word`).
_HEADER_MARKERS = frozenset({
    "№", "n", "п/п", "наименование", "поз.", "поз", "тип", "марка",
    "количество", "примечание", "кол-во", "площадь", "номер",
})
# A row starting with one of these ends the table region -- standard Russian
# drawing title-block vocabulary, not specific to any one document.
_TABLE_END_MARKERS = frozenset({"лист", "формат", "подп.", "изм.", "н.контр.", "гип"})
_MAX_HEADER_ROWS = 2


def new_table_count_budget(pages: int = DEFAULT_PAGE_BUDGET) -> dict[str, int]:
    return {"pages": pages}


def title_candidates_for_stage(param: Param, stage: str) -> list[str]:
    return derive_title_candidates(source_hints(param).get(stage, ""))


def _normalize_row_word(text: object) -> str:
    return str(text or "").lower().replace("ё", "е").strip(" .,;:()")


def _row_y_center(row: list[dict[str, Any]]) -> float:
    return sum((w["bbox"][1] + w["bbox"][3]) / 2 for w in row) / len(row)


def _cluster_rows(words: list[dict[str, Any]], *, y_tolerance: float = _ROW_Y_TOLERANCE) -> list[list[dict[str, Any]]]:
    """Bands words into text-line rows by Y-proximity, each row sorted
    left-to-right by X -- a minimal Y/X-clustering fallback in place of
    PyMuPDF's own table-detection API (see module docstring)."""
    ordered = sorted(words, key=lambda w: (w["bbox"][1], w["bbox"][0]))
    rows: list[list[dict[str, Any]]] = []
    for word in ordered:
        y_center = (word["bbox"][1] + word["bbox"][3]) / 2
        if rows:
            reference = (rows[-1][0]["bbox"][1] + rows[-1][0]["bbox"][3]) / 2
            if abs(y_center - reference) <= y_tolerance:
                rows[-1].append(word)
                continue
        rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["bbox"][0])
    return rows


def _is_header_row(row: list[dict[str, Any]]) -> bool:
    tokens = {_normalize_row_word(w.get("text")) for w in row}
    return bool(tokens & _HEADER_MARKERS)


def _is_table_end_row(row: list[dict[str, Any]]) -> bool:
    first_token = _normalize_row_word(row[0].get("text")) if row else ""
    return first_token in _TABLE_END_MARKERS


@dataclass(slots=True)
class TableCountMatch:
    row_count: int
    table_found: bool
    bbox_pdf: list[float] | None
    header_row_count: int
    fingerprint: TableFingerprint | None = None
    anchor_word_index: int | None = None


def find_table_row_count(snapshot: dict[str, Any], title_candidates: list[str]) -> TableCountMatch | None:
    """Finds the first matching title among `title_candidates` on this page,
    then counts the content rows of the table that (by Y-position) follows
    it. Returns None when no title matches at all -- the caller must keep
    searching other pages, never treat this as "table found, zero rows"."""
    words = snapshot.get("words") or []
    if not words:
        return None
    anchor_end = None
    for title in title_candidates:
        anchor_end = find_anchor_end_index_for_phrase(words, title)
        if anchor_end is not None:
            break
    if anchor_end is None:
        return None
    anchor_word = words[anchor_end - 1]
    anchor_bottom = float(anchor_word["bbox"][3])
    page_height = float(snapshot.get("height") or 0) or 1.0
    height_limit = anchor_bottom + page_height * _MAX_TABLE_HEIGHT_FRACTION
    below = [w for w in words if float(w["bbox"][1]) > anchor_bottom - 0.5 and float(w["bbox"][1]) <= height_limit]
    if not below:
        # Anchor matched but nothing at all follows it on the page -- most
        # likely a stray mention (e.g. a table of contents entry), not the
        # real table. Not a confident "table found" -- let the caller keep
        # looking rather than reporting a real zero.
        return TableCountMatch(row_count=0, table_found=False, bbox_pdf=None, header_row_count=0, anchor_word_index=anchor_end - 1)
    rows = _cluster_rows(below)[:_MAX_ROWS_SCANNED]

    header_row_count = 0
    content_rows: list[list[dict[str, Any]]] = []
    for row in rows:
        if header_row_count < _MAX_HEADER_ROWS and _is_header_row(row):
            header_row_count += 1
            continue
        if _is_table_end_row(row) and content_rows:
            break
        if len(row) >= _MIN_ROW_WORDS:
            content_rows.append(row)

    # A real table structure requires at least a header row OR a plausible
    # multi-column row to have been seen at all -- otherwise this was a
    # stray anchor match with no table beneath it (not a confident "table
    # found, zero content rows").
    table_found = header_row_count > 0 or bool(content_rows)
    if not table_found:
        return TableCountMatch(row_count=0, table_found=False, bbox_pdf=None, header_row_count=0, anchor_word_index=anchor_end - 1)

    all_rows = [row for row in rows[:header_row_count + len(content_rows)]]
    bbox_pdf = union_bbox([w["bbox"] for row in all_rows for w in row]) if all_rows else None
    # Fingerprint the table's own column structure: the detected header
    # row(s) when present, otherwise the first content row as a shape-only
    # fallback (still better than no structural signal at all) -- see
    # `cross_stage_localization.py` for how this is used to disambiguate
    # multiple candidate RD/ID pages once a PD-side table is resolved.
    fingerprint_row = [w for row in rows[:header_row_count] for w in row] if header_row_count else (content_rows[0] if content_rows else None)
    fingerprint = table_fingerprint_from_row(fingerprint_row) if fingerprint_row else None
    return TableCountMatch(
        row_count=len(content_rows), table_found=True, bbox_pdf=bbox_pdf, header_row_count=header_row_count,
        fingerprint=fingerprint, anchor_word_index=anchor_end - 1,
    )


@dataclass(slots=True)
class TableCountObservation:
    row_count: int
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
    # This mechanism counts rows for the table as a whole, never attributing
    # the count to a single room/construction element (see
    # GENERIC_SITE_LOCATION) -- fixed, not per-match.
    location: str = GENERIC_SITE_LOCATION
    # Optional LLM verifier verdict (llm_candidate_verifier.py, via
    # cross_stage_localization.resolve_stage_round) -- None unless
    # CASE10_LLM_VERIFIER_ENABLED; inspector-facing evidence only.
    llm_verification: dict | None = None

    @property
    def display_value(self) -> str:
        return str(self.row_count)


def extract_table_count_observation_for_page(
    title_candidates: list[str],
    document: DocumentVersion,
    page_number: int,
    snapshot: dict[str, Any],
    source_fragment: SourceFragment | None,
) -> TableCountObservation | None:
    if not title_candidates:
        return None
    match = find_table_row_count(snapshot, title_candidates)
    if match is None or not match.table_found or match.bbox_pdf is None:
        return None
    width = float(snapshot.get("width") or 0) or 1.0
    height = float(snapshot.get("height") or 0) or 1.0
    bbox_pdf = match.bbox_pdf
    return TableCountObservation(
        row_count=match.row_count,
        confidence=0.5 if match.row_count > 0 else 0.4,
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
        extractor=GENERIC_TABLE_COUNT_EXTRACTOR_NAME,
        context=f"{match.header_row_count} header row(s), {match.row_count} content row(s) detected below matched title",
        source_fragment=source_fragment,
    )


def counts_equal(left: int, right: int) -> bool:
    return left == right


def eligible_table_count_params(params: Iterable[Param], *, excluded_codes: frozenset[str]) -> list[Param]:
    return [
        param for param in params
        if is_table_count_eligible(code=str(param.code), unit=param.unit, excluded_codes=excluded_codes)
    ]


def _match_table_count(
    snapshot: dict[str, Any], fragment: SourceFragment, doc: DocumentVersion, page: int, titles: list[str],
) -> tuple[TableCountObservation, Any, str | None] | None:
    if not titles:
        return None
    match = find_table_row_count(snapshot, titles)
    if match is None or not match.table_found or match.bbox_pdf is None:
        return None
    observation = extract_table_count_observation_for_page(titles, doc, page, snapshot, fragment)
    if observation is None:
        return None
    semantic_text = row_text_at_word(snapshot.get("words") or [], match.anchor_word_index)
    return observation, match.fingerprint, semantic_text


def collect_table_count_observations(
    params: list[Param],
    by_code: dict[str, list[SourceFragment]],
    by_id: dict[int, DocumentVersion],
    *,
    stage_codes: dict[str, str],
    budget: dict[str, int],
    excluded_codes: frozenset[str],
) -> dict[int, dict[str, TableCountObservation]]:
    """Mirrors `generic_matrix_extraction.collect_generic_observations`'s
    two-round, PD-first shape exactly (see `cross_stage_localization.py`),
    specialized for table-row counting. The anchor searched per stage is
    that stage's own title-candidate list (`title_candidates_for_stage`),
    not the parameter's catalog name -- unlike its three siblings, PD and
    RD/ID titles can differ, so each stage's own titles are threaded through
    as that entry's "extra" payload."""
    candidates_params = eligible_table_count_params(params, excluded_codes=excluded_codes)
    if not candidates_params:
        return {}
    params_by_id = {int(param.id): param for param in candidates_params}

    per_param_by_stage: dict[int, dict[str, list[SourceFragment]]] = {}
    for param in candidates_params:
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

    pd_entries = []
    for param_id, by_stage in per_param_by_stage.items():
        param = params_by_id[param_id]
        if not param.source_pd or "PD" not in by_stage:
            continue
        titles = title_candidates_for_stage(param, "PD")
        if not titles:
            continue
        pd_entries.append((param_id, by_stage["PD"], None, None, titles, build_semantic_query(param, "PD")))
    pd_results = resolve_stage_round(
        entries=pd_entries, by_id=by_id, pages_per_stage=PAGES_PER_STAGE, budget=budget, match_fn=_match_table_count,
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
            titles = title_candidates_for_stage(param, stage)
            if not titles:
                continue
            later_entries.append(((param_id, stage), by_stage[stage], reference_document, reference_fingerprint, titles, build_semantic_query(param, stage)))
    later_results = resolve_stage_round(
        entries=later_entries, by_id=by_id, pages_per_stage=PAGES_PER_STAGE, budget=budget, match_fn=_match_table_count,
    )

    result: dict[int, dict[str, TableCountObservation]] = {}
    for param_id, candidate in pd_results.items():
        result.setdefault(param_id, {})["PD"] = candidate.payload
    for (param_id, stage), candidate in later_results.items():
        result.setdefault(param_id, {})[stage] = candidate.payload
    return {param_id: stages for param_id, stages in result.items() if stages}
