"""Conservative value extraction for the first official CASE10 rule packs.

Only machine-generated locator annotations without check_id are used to select
pages. Values are extracted again from the original, hash-verified PDFs.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import unescape
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Iterable

from ..config import settings
from ..db.models import DocumentVersion, SourceFragment
from .dataset_sources import document_page_count_hint, extract_original_pages, ocr_page_snapshot
from .document_order import document_order_key
from .drawing_classifier import (
    candidate_document_stage,
    classify_ventilation_sheet,
    classify_ventilation_text,
    document_discovery_hints,
    is_ventilation_drawing_page,
    is_candidate_pdf,
    registry_excludes_ventilation,
)
from .room_drawing_compare import (
    AMBIGUITY_MARGIN,
    UNMEASURED_CONFIDENCE,
    WALL_CHECK_STATUS,
    compare_project_to_working,
    detect_sheet_context,
    excluded_zone_rooms,
    extract_room_words,
    group_by_identity,
    keys_compatible,
    match_room_rows,
    normalize_system_label,
    room_system_links,
    room_zone_index,
    warm_floor_rows,
)
from .anchor_vocab import anchor_phrases


SUPPORTED_RULE_CODES = {"PZ-009", "KR-055", "KR-058", "IOS4-078", "IOS4-079"}
_DATASET_STAGE_BY_INTERNAL = {"project": "PD", "working": "RD", "as_built": "ID"}
_MISSING_EXHAUST = "MISSING_EXHAUST"
_SUPPLY_DIMENSION_ONLY_TRIGGER_COUNT = 6


@dataclass(slots=True)
class RuleObservation:
    parameter_code: str
    location: str
    stage: str
    value: str
    normalized_value: str
    confidence: float
    document: DocumentVersion
    page: int
    bbox_normalized: list[float]
    bbox_pdf: list[float]
    page_width: float
    page_height: float
    extractor: str
    context: str
    source_fragment: SourceFragment | None = None
    # `confidence` stays the single legacy number (minimum of the measured ones);
    # these keep recognition and linkage apart (None = not measured).
    text_confidence: float | None = None
    link_confidence: float | None = None


# See official_evidence.py's identically-named `_INFERENCE_SOURCE_SYSTEMS`
# for why both source systems are accepted here (organizer-supplied
# `learning_annotation` training data and this project's own
# `live_candidate_tagger.py` live-scanned equivalent).
_INFERENCE_SOURCE_SYSTEMS = frozenset({"learning_annotation", "live_tagger"})


def is_inference_locator(fragment: SourceFragment) -> bool:
    meta = fragment.metadata_json or {}
    return (
        fragment.source_system in _INFERENCE_SOURCE_SYSTEMS
        and meta.get("status") == "AUTO_FIELD_CANDIDATE"
        and not meta.get("check_id")
        and bool(meta.get("code"))
    )


def _drawing_scan_enabled() -> bool:
    return str(os.environ.get("CASE10_ROOM_DRAWING_COMPARE_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _plan_ventilation_pages(
    documents: dict[int, DocumentVersion],
    candidates: dict[tuple[int, str], dict[int, list[SourceFragment]]],
) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    """Return (known-document scans, first-page probes) within a fixed page budget.

    A registry discipline, a positive organizer section and sheet-code/name
    hints prioritize a PDF, but only page content authorizes a drawing match.
    Unhinted PD/RD files get a small content probe so neutral file names still
    have a route into the classifier.
    """
    full_scans: dict[int, set[int]] = {}
    probes: dict[int, set[int]] = {}
    ranked: list[tuple[int, int, DocumentVersion, tuple[str, ...]]] = []
    for document_id, document in documents.items():
        stage = effective_dataset_stage(document)
        if stage not in {"PD", "RD"} or not is_candidate_pdf(document) or registry_excludes_ventilation(document):
            continue
        hints = document_discovery_hints(document)
        has_locator = any(
            pages for (candidate_id, code), pages in candidates.items()
            if candidate_id == document_id and code in {"IOS4-078", "IOS4-079"}
        )
        if "registry_discipline" in hints:
            rank = 0
        elif "organizer_section" in hints:
            rank = 1
        elif "file_code" in hints:
            rank = 2
        elif "file_title" in hints:
            rank = 3
        elif has_locator:
            rank = 4
        else:
            rank = 5
        ranked.append((rank, document_id, document, hints))

    page_budget = 660
    for rank, document_id, document, _hints in sorted(ranked, key=lambda item: (item[0], item[1])):
        page_count = max(0, document_page_count_hint(document, default=0))
        if page_count <= 0:
            continue
        if rank < 5:
            count = min(page_count, 220, page_budget)
            if count:
                full_scans[document_id] = set(range(1, count + 1))
                page_budget -= count
        else:
            # Bounded first pages probe for documents with neutral filenames and
            # no discipline metadata. A positive content classification expands
            # this document in `_discover_classified_drawing_pages`.
            probes[document_id] = set(range(1, min(page_count, 2) + 1))
    return full_scans, probes


def _discover_classified_drawing_pages(
    documents: dict[int, DocumentVersion],
    full_scans: dict[int, set[int]],
    probes: dict[int, set[int]],
) -> tuple[dict[int, set[int]], int]:
    """Use lightweight text extraction to pick pages before loading word geometry.

    The former implementation materialized word boxes for every discovery page
    (up to 660) before discarding almost all of them. This scans the same bounded
    page ranges for text, then asks the geometry extractor for classified sheets
    only. It keeps page-content classification independent from organizer tags.
    """
    selected_scores: dict[int, dict[int, tuple[int, bool]]] = {}
    text_pages_scanned = 0

    def inspect(document_id: int, pages: set[int]) -> dict[int, tuple[int, bool]]:
        nonlocal text_pages_scanned
        document = documents[document_id]
        try:
            import fitz
            from .dataset_sources import original_document_bytes

            data = original_document_bytes(document)
            hits: dict[int, tuple[int, bool]] = {}
            with fitz.open(stream=data, filetype="pdf") as pdf:
                for page_number in sorted(pages):
                    if page_number < 1 or page_number > len(pdf):
                        continue
                    text_pages_scanned += 1
                    page_text = pdf[page_number - 1].get_text("text")
                    classification = classify_ventilation_text(page_text, document)
                    if classification is None or not is_ventilation_drawing_page(page_text, document):
                        continue
                    signals = set(classification.signals)
                    titled = bool(signals & {"ventilation_title", "heating_title"})
                    # Explicit plan/scheme titles are strong enough for geometry
                    # extraction. Generic room+system co-occurrence is common in
                    # schedules and legends, so it has a smaller per-file fallback.
                    score = (
                        (100 if titled else 0)
                        + (30 if "discipline_stamp" in signals else 0)
                        + (20 if "ventilation_terms" in signals else 0)
                        + (10 if "warm_floor_terms" in signals else 0)
                        + (5 if "room_system_layout" in signals else 0)
                    )
                    hits[page_number] = (score, titled)
            return hits
        except (FileNotFoundError, ValueError, RuntimeError):
            return {}

    for document_id, pages in full_scans.items():
        hits = inspect(document_id, pages)
        if hits:
            selected_scores[document_id] = hits

    for document_id, pages in probes.items():
        probe_hits = inspect(document_id, pages)
        # A neutral filename is expanded only when its first pages contain an
        # explicit HVAC stamp, ventilation/heating title, or HVAC term. A vague
        # room/system coincidence on page 1 is not enough to scan 220 pages.
        if not any(
            titled or score >= 20
            for score, titled in probe_hits.values()
        ):
            continue
        document = documents[document_id]
        page_count = max(0, document_page_count_hint(document, default=0))
        expand = set(range(1, min(page_count, 220) + 1)) - set(pages)
        hits = inspect(document_id, expand)
        selected_scores.setdefault(document_id, {}).update(probe_hits | hits)

    # Keep every explicit plan/scheme page (up to a safety ceiling), then a
    # bounded layout-only fallback for older sheets without a searchable title.
    # This is object-agnostic and prevents drawing stamps on legends/specs from
    # turning a live run into hundreds of full geometry/OCR extractions.
    selected: dict[int, set[int]] = {}
    for document_id, scores in selected_scores.items():
        titled_pages = sorted(
            ((score, page) for page, (score, titled) in scores.items() if titled),
            key=lambda row: (-row[0], row[1]),
        )[:32]
        titled_set = {page for _score, page in titled_pages}
        fallback_pages = sorted(
            ((score, page) for page, (score, titled) in scores.items() if not titled),
            key=lambda row: (-row[0], row[1]),
        )[:12]
        pages = titled_set | {page for _score, page in fallback_pages}
        if pages:
            selected[document_id] = pages
    return selected, text_pages_scanned


def effective_dataset_stage(document: DocumentVersion) -> str:
    raw = str(document.dataset_stage or "").upper()
    if raw in {"PD", "RD", "ID"}:
        return raw
    return _DATASET_STAGE_BY_INTERNAL.get(str(document.doc_stage or ""), raw or "UNKNOWN")


def extract_official_rule_observations(
    docs: Iterable[DocumentVersion],
    fragments: Iterable[SourceFragment],
    *,
    only_codes: set[str] | None = None,
) -> tuple[dict[str, list[RuleObservation]], list[dict[str, Any]]]:
    """`only_codes`, when given, restricts extraction (and therefore all the
    expensive page rendering/OCR below it) to that subset of
    `SUPPORTED_RULE_CODES`. An incremental recompute for a single affected
    parameter passes its own code here so it does not pay the full-dataset
    page-scan cost of the other, unaffected rule packs -- this is what makes
    an impact-scoped `run_process` call genuinely cheaper than a full one,
    not just narrower in which EvidenceGroup rows it touches."""
    active_codes = (SUPPORTED_RULE_CODES & only_codes) if only_codes is not None else SUPPORTED_RULE_CODES
    documents = {int(doc.id): doc for doc in docs}
    candidates: dict[tuple[int, str], dict[int, list[SourceFragment]]] = {}
    all_page_fragments: dict[tuple[int, int], list[SourceFragment]] = {}
    for fragment in fragments:
        if not is_inference_locator(fragment) or not fragment.page:
            continue
        document_id = int(fragment.document_version_id)
        code = str((fragment.metadata_json or {}).get("code") or "")
        if document_id not in documents:
            continue
        all_page_fragments.setdefault((document_id, int(fragment.page)), []).append(fragment)
        if code in active_codes:
            candidates.setdefault((document_id, code), {}).setdefault(int(fragment.page), []).append(fragment)

    drawing_full_scans: dict[int, set[int]] = {}
    drawing_probes: dict[int, set[int]] = {}
    drawing_text_pages_scanned = 0
    if _drawing_scan_enabled() and active_codes & {"IOS4-078", "IOS4-079"}:
        drawing_full_scans, drawing_probes = _plan_ventilation_pages(documents, candidates)
        drawing_pages, drawing_text_pages_scanned = _discover_classified_drawing_pages(
            documents, drawing_full_scans, drawing_probes,
        )
        # Upstream locators help discover source files, but their page hints may
        # point to unrelated schedules/specifications. The room comparison reads
        # only pages confirmed as plans/schemes by their content.
        for document_id, code in list(candidates):
            if code in {"IOS4-078", "IOS4-079"}:
                selected_pages = drawing_pages.get(document_id, set())
                candidates[(document_id, code)] = {
                    page: rows for page, rows in candidates[(document_id, code)].items()
                    if page in selected_pages
                }
        for document_id, pages in drawing_pages.items():
            for code in ("IOS4-078", "IOS4-079"):
                if code in active_codes:
                    page_map = candidates.setdefault((document_id, code), {})
                    for page in pages:
                        page_map.setdefault(page, [])

    # A page pre-tagged for one structural/site value parameter often repeats other
    # such parameters in the same notes or legend (e.g. a general-notes page carries
    # both the site elevation and the concrete class table). Scan the union of
    # already-flagged pages for every value code sharing a document, instead of only
    # the pages the upstream heuristic happened to tag with that exact code, so a
    # tagging gap for one code does not hide evidence that is genuinely present.
    value_codes = active_codes - {"IOS4-078", "IOS4-079"}
    union_pages_by_document: dict[int, dict[int, list[SourceFragment]]] = defaultdict(dict)
    for (document_id, code), pages in candidates.items():
        if code not in value_codes:
            continue
        merged = union_pages_by_document[document_id]
        for page, rows in pages.items():
            merged[page] = merged.get(page, []) + rows
    for document_id, merged in union_pages_by_document.items():
        for code in value_codes:
            candidates[(document_id, code)] = merged

    pages_by_document: dict[int, set[int]] = {}
    for (document_id, code), pages in candidates.items():
        limit = 220 if code in {"IOS4-078", "IOS4-079"} else 80
        pages_by_document.setdefault(document_id, set()).update(_prioritized_pages(pages, limit=limit))

    snapshots: dict[tuple[int, int], dict] = {}
    for document_id, pages in pages_by_document.items():
        try:
            extracted = extract_original_pages(documents[document_id], pages)
        except (FileNotFoundError, ValueError, RuntimeError):
            continue
        for page_number, snapshot in extracted.items():
            snapshots[(document_id, int(page_number))] = snapshot

    output = {code: [] for code in active_codes}
    for (document_id, code), pages in candidates.items():
        document = documents[document_id]
        stage = effective_dataset_stage(document)
        if stage not in {"PD", "RD", "ID"}:
            continue
        for page_number in _prioritized_pages(pages):
            snapshot = snapshots.get((document_id, page_number))
            if not snapshot:
                continue
            page_fragments = all_page_fragments.get((document_id, page_number), [])
            source_fragment = sorted(
                pages[page_number], key=lambda row: (-(row.confidence or 0), row.id or 0)
            )[0] if pages[page_number] else None
            output[code].extend(
                parse_rule_page(
                    code,
                    snapshot,
                    document=document,
                    stage=stage,
                    source_fragment=source_fragment,
                    page_fragments=page_fragments,
                )
            )

    room_identity = new_room_identity_report()
    for code in ("IOS4-078", "IOS4-079"):
        if code not in active_codes:
            continue
        output[code].extend(_extract_ventilation_observations(code, documents, candidates, snapshots, room_identity))

    warm_floor_status: dict[str, Any] = {"code": "FREE_SEARCH", "project_rooms": 0, "compared_rooms": 0, "rooms": []}
    if active_codes & {"IOS4-078", "IOS4-079"}:
        floor_observations, warm_floor_status = _extract_warm_floor_observations(documents, snapshots, room_identity)
        if floor_observations:
            output["FREE_SEARCH"] = _deduplicate(floor_observations)

    for code, observations in output.items():
        output[code] = _deduplicate(observations)
    return output, {
        "documents": documents,
        "scanned_pages": set(snapshots.keys()),
        "warm_floor_comparison": warm_floor_status,
        "room_identity": room_identity,
        "drawing_scan_enabled": _drawing_scan_enabled(),
        "drawing_text_pages_scanned": drawing_text_pages_scanned,
    }


def new_fallback_budget() -> dict[str, int]:
    """One shared, mutable budget for a whole process run: every missing-stage
    gap across every param draws from the SAME pool, so N gaps cannot multiply
    into N times the per-gap cost."""
    return {
        "documents": settings.RULE_FALLBACK_MAX_DOCUMENTS,
        "pages": settings.RULE_FALLBACK_MAX_PAGES_TOTAL,
        "ocr_pages": settings.RULE_FALLBACK_MAX_OCR_PAGES,
    }


def find_confirming_fallback_observation(
    code: str,
    stage: str,
    *,
    location: str,
    expected_normalized_value: str,
    documents: dict[int, DocumentVersion],
    exclude_pages: set[tuple[int, int]],
    budget: dict[str, int],
    sparse_page_word_threshold: int = 20,
    max_pages_this_attempt: int | None = None,
) -> tuple["RuleObservation | None", dict[str, Any]]:
    """Bounded discovery for evidence of a value PD/RD (or RD/ID) already agree
    on, on the one stage the fast, upstream-tag-based path found nothing for.

    This is confirm-only by construction, not a free rediscovery: a candidate is
    accepted only if its extracted `location` and `normalized_value` match what
    the other stages already established. A mismatching value found on some
    unrelated document is discarded, never surfaced as a new value or a new
    violation — it cannot flip an already-made decision or manufacture one.
    Documents are selected structurally by stage only (not by MATRIX_FIELD/
    value-code tagging), in ascending document id order (the dataset's own
    registry order, not derived from any evidence or gold label). OCR is tried
    only on pages whose text layer is suspiciously sparse, and only within the
    same shared, mutable `budget` (documents/pages/ocr_pages all draw from it).

    `max_pages_this_attempt` caps how many of the shared page budget THIS one
    (parameter, stage, location) gap may draw, so a single expensive gap early
    in a process run cannot exhaust the whole pool and starve every gap after
    it (empirically observed: one gap alone consumed the entire prior 80-page
    budget before a later, ultimately findable gap ever got to run).
    """
    started = time.monotonic()
    documents_scanned = 0
    pages_scanned = 0
    ocr_pages_scanned = 0
    found: RuleObservation | None = None
    attempt_cap = settings.RULE_FALLBACK_MAX_PAGES_PER_ATTEMPT if max_pages_this_attempt is None else max_pages_this_attempt
    attempt_pages_remaining = min(budget["pages"], attempt_cap) if attempt_cap > 0 else budget["pages"]
    if budget["documents"] > 0 and attempt_pages_remaining > 0:
        candidate_docs = sorted(
            (doc for doc in documents.values() if effective_dataset_stage(doc) == stage),
            key=document_order_key,
        )
        for document in candidate_docs:
            if found or budget["documents"] <= 0 or attempt_pages_remaining <= 0:
                break
            page_count = document_page_count_hint(document)
            pages = [
                page for page in range(1, min(page_count, attempt_pages_remaining) + 1)
                if (int(document.id), page) not in exclude_pages
            ]
            if not pages:
                continue
            budget["documents"] -= 1
            documents_scanned += 1
            try:
                snapshots = extract_original_pages(document, pages)
            except (FileNotFoundError, ValueError, RuntimeError):
                continue
            budget["pages"] -= len(pages)
            attempt_pages_remaining -= len(pages)
            pages_scanned += len(pages)
            sparse_pages = []
            for page_number, snapshot in snapshots.items():
                found = _matching_observation(
                    code, snapshot, document=document, stage=stage,
                    location=location, expected_normalized_value=expected_normalized_value,
                )
                if found:
                    break
                if len(snapshot.get("words") or []) < sparse_page_word_threshold:
                    sparse_pages.append(page_number)
            if not found and sparse_pages and budget["ocr_pages"] > 0:
                for page_number in sparse_pages:
                    if budget["ocr_pages"] <= 0:
                        break
                    budget["ocr_pages"] -= 1
                    ocr_pages_scanned += 1
                    ocr_snapshot = ocr_page_snapshot(document, page_number)
                    if not ocr_snapshot:
                        continue
                    found = _matching_observation(
                        code, ocr_snapshot, document=document, stage=stage,
                        location=location, expected_normalized_value=expected_normalized_value,
                    )
                    if found:
                        break
    diagnostics = {
        "code": code,
        "stage": stage,
        "location": location,
        "documents_scanned": documents_scanned,
        "pages_scanned": pages_scanned,
        "ocr_pages_scanned": ocr_pages_scanned,
        "found": found is not None,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    return found, diagnostics


def _matching_observation(
    code: str,
    snapshot: dict[str, Any],
    *,
    document: DocumentVersion,
    stage: str,
    location: str,
    expected_normalized_value: str,
) -> "RuleObservation | None":
    for observation in parse_rule_page(code, snapshot, document=document, stage=stage, source_fragment=None, page_fragments=()):
        if observation.location == location and observation.normalized_value == expected_normalized_value:
            return observation
    return None


def parse_rule_page(
    code: str,
    snapshot: dict[str, Any],
    *,
    document: DocumentVersion,
    stage: str,
    source_fragment: SourceFragment | None = None,
    page_fragments: Iterable[SourceFragment] = (),
) -> list[RuleObservation]:
    if code == "PZ-009":
        rows = _extract_absolute_zero(snapshot)
    elif code == "KR-055":
        rows = _extract_concrete_classes(snapshot, page_fragments, document=document)
    elif code == "KR-058":
        rows = _extract_foundation_thickness(snapshot, stage=stage)
    else:
        return []
    return [
        RuleObservation(
            parameter_code=code,
            location=row["location"],
            stage=stage,
            value=row["value"],
            normalized_value=row["normalized_value"],
            confidence=row["confidence"],
            document=document,
            page=int(snapshot["page"]),
            bbox_normalized=_normalized_bbox(row["bbox_pdf"], snapshot["width"], snapshot["height"]),
            bbox_pdf=row["bbox_pdf"],
            page_width=float(snapshot["width"]),
            page_height=float(snapshot["height"]),
            extractor=f"official_rule:{code.lower()}",
            context=row["context"],
            source_fragment=source_fragment,
        )
        for row in rows
    ]


def rule_is_violation(code: str, expected: str, actual: str) -> bool | None:
    if code == "PZ-009":
        left, right = _decimal(expected), _decimal(actual)
        return None if left is None or right is None else left != right
    if code == "KR-055":
        left, right = _decimal(expected.removeprefix("B")), _decimal(actual.removeprefix("B"))
        return None if left is None or right is None else right < left
    if code == "KR-058":
        left, right = _number_list(expected), _number_list(actual)
        return None if not left or not right else min(right) < min(left)
    if code == "IOS4-078":
        return _is_exhaust_violation(expected, actual)
    if code == "IOS4-079":
        return _is_supply_violation(expected, actual)
    return None


def _prioritized_pages(pages: dict[int, list[SourceFragment]], limit: int = 80) -> list[int]:
    def priority(item):
        page, rows = item
        has_number = any(re.search(r"\d", str(row.text or "")) for row in rows)
        confidence = max((float(row.confidence or 0) for row in rows), default=0.0)
        return (not has_number, -confidence, page)

    return [page for page, _rows in sorted(pages.items(), key=priority)[:limit]]


# Decision semantics per code (see the trigger texts in trigger_policy.py):
#  IOS4-078 "Уменьшение площади сечения воздуховода в РД" -> a reduction: the
#           comparison decides on systems that disappeared; additions are not a violation.
#  IOS4-079 "Подмена установок на аналоги ..." -> a substitution: any change of the
#           installation set is relevant, so missing and added are both reported.
# In both modes the added and the missing systems are kept apart in the report.
ROOM_COMPARISON_MODE = {"IOS4-078": "disappearance", "IOS4-079": "composition"}


def new_room_identity_report() -> dict[str, Any]:
    return {
        "comparison_mode": dict(ROOM_COMPARISON_MODE),
        "wall_check": WALL_CHECK_STATUS,
        "issues": [], "added_systems": [], "ambiguous_links": [], "excluded_zone_rooms": [],
        "pairs_compared": 0,
    }


def _location_labels(keys: Iterable[tuple[str, str, str, str]]) -> dict[tuple[str, str, str, str], str]:
    """Observation location per room key: the printed number, extended only when
    the same number belongs to several rooms (so groups downstream never collide)."""
    keys = sorted(set(keys))
    count: dict[str, int] = defaultdict(int)
    for key in keys:
        count[key[3]] += 1
    labels = {}
    for key in keys:
        building, floor, scope, number = key
        if count[number] == 1:
            labels[key] = number
            continue
        parts = []
        if building:
            parts.append(f"корпус {building}")
        if floor:
            parts.append(f"этаж {floor}")
        if not parts:
            document_id, _, page = scope.partition("|")
            parts.append(f"док. {document_id}, лист {page}" if document_id else f"лист {page}")
        labels[key] = f"{number} ({', '.join(parts)})"
    return labels


def _issue_source(stage: str, document: DocumentVersion | None, row: dict[str, Any]) -> dict[str, Any]:
    snapshot = row.get("snapshot") or {}
    return {
        "stage": stage,
        "document_id": int(document.id) if document is not None else None,
        "dataset_file_id": getattr(document, "dataset_file_id", None),
        "page": int(row.get("sheet_page") or snapshot.get("page") or 0) or None,
        "bbox_pdf": row.get("bbox_pdf"),
        "page_width": snapshot.get("width"), "page_height": snapshot.get("height"),
        "systems": list(row.get("systems") or ()),
        "floor": row.get("floor"), "building": row.get("building"),
    }


def _room_issue(code, reason, location, sources, text, **extra) -> dict[str, Any]:
    return {"code": code, "status": "CLARIFICATION_REQUIRED", "reason": reason, "location": location,
            "sources": sources, "text": text, **extra}


# Minimum number of shared room numbers (and the factor over the runner-up) for a project sheet to be
# preferred over another project sheet that also carries the same room number but no determinable floor.
_SHEET_OVERLAP_MIN = 3
_SHEET_OVERLAP_FACTOR = 2.0


def _sheet_room_numbers(snapshot: dict[str, Any]) -> set[str]:
    """Room numbers of a sheet; tokens repeated more than twice are dimensions/axes, not rooms."""
    counts: dict[str, int] = defaultdict(int)
    for room in extract_room_words(snapshot):
        counts[room["room"]] += 1
    return {number for number, count in counts.items() if count <= 2}


def _floors_known_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(left.get("floor")) and left.get("floor") == right.get("floor") and (
        (left.get("building") or "") == (right.get("building") or ""))


def _extract_ventilation_observations(
    code: str,
    documents: dict[int, DocumentVersion],
    candidates: dict[tuple[int, str], dict[int, list[SourceFragment]]],
    snapshots: dict[tuple[int, int], dict],
    identity_report: dict[str, Any] | None = None,
) -> list[RuleObservation]:
    report = identity_report if identity_report is not None else new_room_identity_report()
    mode = ROOM_COMPARISON_MODE.get(code, "disappearance")
    pd_rows: list[dict[str, Any]] = []
    pd_source: dict[int, tuple[DocumentVersion, SourceFragment | None]] = {}
    for (document_id, candidate_code), pages in candidates.items():
        document = documents[document_id]
        if candidate_code != code or effective_dataset_stage(document) != "PD":
            continue
        for page_number in _prioritized_pages(pages, limit=220):
            snapshot = snapshots.get((document_id, page_number))
            if not snapshot or not _is_relevant_pd_ventilation_sheet(code, snapshot, document):
                continue
            links = _pd_exhaust_room_links(snapshot) if code == "IOS4-078" else _pd_supply_room_links(snapshot)
            source_fragment = _best_fragment(pages.get(page_number, []))
            for row in links["rows"]:
                row["sheet"] = str(document_id)
                pd_rows.append(row)
                pd_source[id(row)] = (document, source_fragment)
            for item in links["ambiguous"]:
                report["ambiguous_links"].append({"code": code, "stage": "PD", "document_id": document_id,
                                                  "page": page_number, **{k: v for k, v in item.items() if k != "bbox"}})
            if links["excluded_zone_rooms"]:
                report["excluded_zone_rooms"].append({"code": code, "stage": "PD", "document_id": document_id,
                                                      "page": page_number, "rooms": links["excluded_zone_rooms"]})

    if not pd_rows:
        return []

    pd_groups = group_by_identity(pd_rows)
    labels = _location_labels(group["key"] for group in pd_groups)   # for issues; observations get their own labels
    expected: dict[tuple[str, str, str, str], tuple[dict[str, Any], DocumentVersion, SourceFragment | None]] = {}
    for group in pd_groups:
        first_row = group["records"][0]
        if group["conflict"]:
            report["issues"].append(_room_issue(
                code, "DUPLICATE_ROOM_KEY", labels[group["key"]],
                [_issue_source("PD", pd_source[id(r)][0], r) for r in group["records"]],
                f"В ПД помещение {first_row['location']} с одним и тем же ключом (корпус, этаж, номер) "
                "описано разными наборами систем; сравнение не выполнялось.",
                stage="PD", building=group["key"][0], floor=group["key"][1],
            ))
            continue
        row = group["row"]
        document, source_fragment = pd_source[id(row)]
        expected[group["key"]] = (row, document, source_fragment)

    if not expected:
        return []

    rd_pages = []
    expected_numbers = {key[3] for key in expected}
    project_signatures = [value[0] for value in expected.values()]
    for (document_id, page_number), snapshot in snapshots.items():
        document = documents[document_id]
        if effective_dataset_stage(document) != "RD" or not _is_rd_ventilation_drawing_document(code, document, snapshot):
            continue
        zones = room_zone_index(snapshot)
        overlap = {zone["location"] for zone in zones} & expected_numbers
        if not overlap:
            continue
        links = _pd_exhaust_room_links(snapshot) if code == "IOS4-078" else _pd_supply_room_links(snapshot)
        for row in links["rows"]:
            row["sheet"] = str(document_id)
        for zone in zones:
            zone["sheet"] = str(document_id)
        entries = compare_project_to_working(project_signatures, links["rows"], mode="composition")
        changed_rooms = {entry["location"] for entry in entries
                         if entry["status"] in {"MISSING", "ADDED", "MISSING_AND_ADDED"}}
        classification = classify_ventilation_sheet(snapshot, document)
        locations = {zone["location"] for zone in zones}
        score = (len(overlap) * 100 + len(changed_rooms & overlap) * 40
                 + (classification.confidence if classification else 0) * 10 + min(len(locations), 50))
        rd_pages.append({"score": score, "document": document, "snapshot": snapshot, "zones": zones,
                         "native": links["rows"], "links": links})
        for item in links["ambiguous"]:
            report["ambiguous_links"].append({"code": code, "stage": "RD", "document_id": document_id,
                                              "page": page_number, **{k: v for k, v in item.items() if k != "bbox"}})
        if links["excluded_zone_rooms"]:
            report["excluded_zone_rooms"].append({"code": code, "stage": "RD", "document_id": document_id,
                                                  "page": page_number, "rooms": links["excluded_zone_rooms"]})

    # Pass 1: find, for every project room, the working sheet/room it can be compared with.
    plans: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for key in sorted(expected):
        row, document, _source_fragment = expected[key]
        number = key[3]
        plan: dict[str, Any] = {"state": "go"}
        plans[key] = plan
        page_matches = []
        for page in rd_pages:
            same_number = [zone for zone in page["zones"] if zone["location"] == number and keys_compatible(row, zone)]
            if same_number:
                page_matches.append((page, same_number))
        if not page_matches:
            plan["state"] = "no_rd"
            continue
        known_places = {(z.get("building") or "", z.get("floor") or "") for _p, zs in page_matches for z in zs}
        if (not key[1] or not key[0]) and len({place for place in known_places if place[1] or place[0]}) > 1:
            report["issues"].append(_room_issue(
                code, "AMBIGUOUS_ROOM_MATCH", labels[key],
                [_issue_source("PD", document, row)] + [
                    _issue_source("RD", page["document"], zs[0]) for page, zs in page_matches],
                f"Помещение {number} в ПД без определённого этажа/корпуса совпадает по номеру с помещениями "
                "разных этажей в РД; соответствие не выбиралось.",
                stage="RD",
            ))
            plan["state"] = "issue"
            continue
        selected_page, room_zones = max(
            page_matches,
            key=lambda item: (item[0]["score"], float(item[0]["snapshot"]["width"]) * float(item[0]["snapshot"]["height"]),
                              -int(item[0]["snapshot"]["page"])),
        )
        plan.update(selected_page=selected_page, room_zones=room_zones)
        rd_document, rd_snapshot = selected_page["document"], selected_page["snapshot"]
        # Another project room with the same number may claim the same working room (the same number on
        # a sheet of another floor).  A floor known on both sides decides; otherwise the project sheet
        # that shares clearly more room numbers with the working sheet; otherwise nobody is chosen.
        competitors = [
            other_key for other_key in sorted(expected)
            if other_key != key and other_key[3] == number
            and any(keys_compatible(expected[other_key][0], zone) for zone in room_zones)
        ]
        if not competitors:
            continue
        own_strong = any(_floors_known_equal(row, zone) for zone in room_zones)
        rival_strong = [k for k in competitors
                        if any(_floors_known_equal(expected[k][0], zone) for zone in room_zones)]
        rd_numbers = _sheet_room_numbers(rd_snapshot) - {number}
        own_overlap = len((_sheet_room_numbers(row["snapshot"]) - {number}) & rd_numbers)
        rival_overlap = max((len((_sheet_room_numbers(expected[k][0]["snapshot"]) - {number}) & rd_numbers)
                             for k in competitors), default=0)
        no_floor_evidence = not own_strong and not rival_strong
        if own_strong and not rival_strong:
            resolution = "floor"
        elif rival_strong and not own_strong:
            resolution = "lost_to_floor"
        elif no_floor_evidence and own_overlap >= _SHEET_OVERLAP_MIN                 and own_overlap >= _SHEET_OVERLAP_FACTOR * rival_overlap:
            resolution = "sheet_overlap"
        elif no_floor_evidence and rival_overlap >= _SHEET_OVERLAP_MIN                 and rival_overlap >= _SHEET_OVERLAP_FACTOR * max(own_overlap, 1):
            resolution = "lost_to_overlap"
        else:
            resolution = "unresolved"
        report.setdefault("room_resolutions", []).append({
            "code": code, "location": labels[key], "resolution": resolution,
            "own_overlap": own_overlap, "rival_overlap": rival_overlap,
            "competitors": [labels[k] for k in competitors],
        })
        if resolution.startswith("lost_to"):
            # the working room belongs to another project sheet, which compares it in its own pass
            plan["state"] = "lost"
        elif resolution == "unresolved":
            report["issues"].append(_room_issue(
                code, "AMBIGUOUS_ROOM_MATCH", labels[key],
                [_issue_source("PD", document, row)]
                + [_issue_source("PD", expected[k][1], expected[k][0]) for k in competitors]
                + [_issue_source("RD", rd_document, zone) for zone in room_zones],
                f"Помещение {number} есть на нескольких листах ПД, этаж которых не определён, и совпадает по "
                "номеру с помещением РД; соответствие по общим номерам листа не выделено, сравнение не выполнялось.",
                stage="RD", competitors=[labels[k] for k in competitors],
            ))
            plan["state"] = "issue"

    # Pass 2: observations.  A project room that lost its working counterpart to another sheet is not
    # emitted alone (a project-only group would only be noise); labels are extended only where needed.
    emit_keys = [key for key in sorted(expected) if plans[key]["state"] != "lost"]
    labels = _location_labels(emit_keys)
    observations = []
    for key in emit_keys:
        row, document, source_fragment = expected[key]
        location = labels[key]
        number = key[3]
        observations.append(_ventilation_observation(
            code, {**row, "location": location}, document, row["snapshot"], "PD", source_fragment,
            extractor=f"official_rule:{code.lower()}-layout",
        ))
        if plans[key]["state"] != "go":
            continue
        selected_page, room_zones = plans[key]["selected_page"], plans[key]["room_zones"]
        rd_document, rd_snapshot = selected_page["document"], selected_page["snapshot"]
        zone_groups = group_by_identity(room_zones)
        native_candidates = [r for r in selected_page["native"] if r["location"] == number and keys_compatible(row, r)]
        native_groups = group_by_identity(native_candidates)
        unplaceable = len(room_zones) > 1 and any(not zone.get("systems") for zone in room_zones)
        if any(g["conflict"] for g in [*zone_groups, *native_groups]) or len(zone_groups) > 1 or len(native_groups) > 1 or unplaceable:
            report["issues"].append(_room_issue(
                code, "DUPLICATE_ROOM_KEY", location,
                [_issue_source("PD", document, row)] + [_issue_source("RD", rd_document, z) for z in room_zones],
                f"В РД на листе номер {number} встречается несколько раз с одним ключом; "
                "по положению подписи помещение не определить, сравнение не выполнялось.",
                stage="RD",
            ))
            continue
        room_zone = zone_groups[0]["row"]
        local_row = native_groups[0]["row"] if native_groups else None
        room_box = room_zone["room_bbox"]
        scan_clip = _centered_clip(
            room_box, rd_snapshot["width"], rd_snapshot["height"],
            max(260.0, float(rd_snapshot["width"]) * 0.11),
            max(240.0, float(rd_snapshot["height"]) * 0.07),
        )
        evidence_bbox = room_zone["bbox_pdf"]
        # Digit/dimension crops (duct sizes like "200x100") are calibrated against
        # English-only OCR; Cyrillic mode adds visually-similar letter/digit
        # confusions here without adding value, since there is no prose to read.
        clip_text = _text_in_bbox(rd_snapshot, scan_clip)
        if local_row is None and code == "IOS4-078":
            # Branch comparison must retain an explicitly printed trunk label
            # (for example V3) as the actual value when PD lists V3.1/V3.2.
            zone_systems = [
                label for label in room_zone.get("systems", ())
                if (normalized := normalize_system_label(str(label)))
                and normalized[0] in {"branch", "exhaust_system"}
            ]
            if zone_systems:
                local_row = {**room_zone, "systems": zone_systems}
        ocr_text = ""
        if local_row is None:
            # OCR only when the page text layer did not attach a system to this
            # room. Re-OCRing every already-readable room multiplied the number
            # of external OCR calls by the number of rooms on a plan sheet.
            ocr_words, ocr_text = _ocr_room_system_words(rd_document, int(rd_snapshot["page"]), scan_clip, code)
            geometry_snapshot = {
                **rd_snapshot,
                "words": [*(rd_snapshot.get("words") or []), *ocr_words],
            }
            system_kinds = {"branch", "exhaust_system"} if code == "IOS4-078" else {"supply"}
            ocr_links = room_system_links(geometry_snapshot, kinds=system_kinds)
            ambiguous_here = [item for item in ocr_links["ambiguous"]
                              if any(c["location"] == number for c in item["candidates"])]
            selected_page["links"] = {**selected_page["links"],
                                      "ambiguous": [*selected_page["links"]["ambiguous"], *ambiguous_here]}
            local_row = next((candidate for candidate in ocr_links["rows"]
                              if candidate["location"] == number and keys_compatible(row, candidate)), None)
            if local_row is not None:
                local_row["sheet"] = str(rd_document.id)
        # An expected label whose room link on the RD sheet was ambiguous is neither
        # present nor missing: no verdict (otherwise it would be reported as "disappeared").
        unsure_labels = set(local_row.get("ambiguous_systems") or ()) if local_row else set()
        for item in selected_page["links"]["ambiguous"]:
            if any(c["location"] == number for c in item["candidates"]):
                unsure_labels.add(item["label"])
        assigned_labels = set(local_row["systems"]) if local_row else set()
        unresolved_expected = (set(row.get("systems") or ()) & unsure_labels) - assigned_labels
        evidence_text = "\n".join(part for part in (clip_text, ocr_text) if part)
        if code == "IOS4-078":
            normalized = ",".join(local_row["systems"]) if local_row else _extract_exhaust_systems(clip_text)
        else:
            normalized = ",".join(local_row["systems"]) if local_row else _extract_supply_configuration(evidence_text)
        if local_row:
            evidence_bbox = local_row["bbox_pdf"]
        if code == "IOS4-078" and not normalized:
            if not evidence_text:
                # Without readable room-zone text there is no source evidence to compare.
                continue
            normalized = _MISSING_EXHAUST
            value = "Вытяжная система не распознана в зоне помещения"
        elif not normalized:
            continue
        else:
            value = _ventilation_value(code, normalized)
        if unresolved_expected:
            # Ambiguous labels block the verdict only when they could change it: compare the rule's answer
            # with the labels left out and with them assigned to this room.
            held = {label for label in normalized.split(",") if label and label != _MISSING_EXHAUST}
            with_labels = ",".join(sorted(held | unresolved_expected, key=_natural_system_key))
            without_result = rule_is_violation(code, row["normalized_value"], normalized)
            with_result = rule_is_violation(code, row["normalized_value"], with_labels)
            if without_result is None or without_result != with_result:
                report["issues"].append(_room_issue(
                    code, "AMBIGUOUS_SYSTEM_LINK", location,
                    [_issue_source("PD", document, row), _issue_source("RD", rd_document, room_zone)],
                    f"В РД подпись системы {', '.join(sorted(unresolved_expected))} стоит между помещениями, "
                    f"включая {number}; привязка не назначена, от неё зависит вердикт, поэтому он не выносится.",
                    stage="RD", unresolved_systems=sorted(unresolved_expected),
                ))
                continue
            report.setdefault("ambiguity_not_decisive", []).append({
                "code": code, "location": location, "unresolved_systems": sorted(unresolved_expected),
                "verdict": bool(without_result)})
        # Measured confidences only. A value read from the text of the room clip
        # (no label-to-room linkage performed) has no measured score at all.
        if local_row:
            text_confidence, link_confidence = local_row.get("text_confidence"), local_row.get("link_confidence")
            confidence = local_row.get("confidence")
        else:
            text_confidence = link_confidence = None
            confidence = None
        if confidence is None:
            confidence = UNMEASURED_CONFIDENCE
        actual = {
            "location": location,
            "value": value,
            "normalized_value": normalized,
            "bbox_pdf": evidence_bbox,
            "confidence": confidence,
            "text_confidence": text_confidence, "link_confidence": link_confidence,
            "linkage": local_row.get("linkage") if local_row else None,
            "context": f"Лист ОВ классифицирован по содержимому; зона помещения {number}:\n{evidence_text[:1200]}",
        }
        observations.append(_ventilation_observation(
            code, actual, rd_document, rd_snapshot, "RD", None,
            extractor=f"official_rule:{code.lower()}-ocr",
        ))
        report["pairs_compared"] += 1
        if local_row is not None:
            comparison = compare_project_to_working([row], [local_row], mode="composition", include_unresolved=True)
            if comparison and comparison[0].get("added_systems"):
                report["added_systems"].append({
                    "code": code, "type": "added", "violation_claimed": False,
                    "decision_mode": mode, "location": location,
                    "added_systems": comparison[0]["added_systems"],
                    "missing_systems": comparison[0]["missing_systems"],
                    "match_basis": comparison[0].get("match_basis"),
                    "PD": _issue_source("PD", document, row), "RD": _issue_source("RD", rd_document, local_row),
                    "text_confidence": text_confidence, "link_confidence": link_confidence,
                })
    return observations


def _ocr_room_system_words(
    document: DocumentVersion,
    page_number: int,
    bbox_pdf: list[float],
    code: str,
) -> tuple[list[dict[str, Any]], str]:
    """OCR a room clip with word boxes so system labels stay attached to rooms.

    The crop is small and uses the same source-page frame as the room label.
    hOCR works with both current Tesseract and older deployments that cannot
    read image bytes from stdin.
    """
    if len(bbox_pdf) != 4:
        return [], ""
    try:
        import fitz
        from PIL import Image
        from io import BytesIO
        from .dataset_sources import original_document_bytes

        data = original_document_bytes(document)
        with fitz.open(stream=data, filetype="pdf") as pdf:
            if page_number < 1 or page_number > len(pdf):
                return [], ""
            page = pdf[page_number - 1]
            clip = fitz.Rect(bbox_pdf) & page.rect
            if clip.is_empty:
                return [], ""
            png = page.get_pixmap(
                matrix=fitz.Matrix(2, 2), clip=clip, alpha=False, annots=False,
            ).tobytes("png")
        image = Image.open(BytesIO(png)).convert("L")
        image = image.point(lambda value: 0 if value < 190 else 255)
        with tempfile.TemporaryDirectory(prefix="case10_room_ocr_") as tmp_dir:
            image_path = Path(tmp_dir) / "room.png"
            output_base = Path(tmp_dir) / "room"
            image.save(image_path, format="PNG")
            result = subprocess.run(
                ["tesseract", str(image_path), str(output_base), "-l", "eng", "hocr"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            output_path = output_base.with_suffix(".html")
            if result.returncode != 0 or not output_path.exists():
                return [], ""
            markup = output_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return [], ""

    clip_rect = clip
    words = []
    raw_tokens = []
    word_spans = re.finditer(
        r"<span\b[^>]*class=['\"]ocrx_word['\"][^>]*>.*?</span>", markup, re.IGNORECASE | re.DOTALL,
    )
    for match in word_spans:
        span = match.group(0)
        coords = re.search(
            r"title=['\"]bbox\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)", span,
            re.IGNORECASE,
        )
        if not coords:
            continue
        body = span[span.find(">") + 1:span.rfind("</span>")]
        raw_text = unescape(re.sub(r"<[^>]+>", "", body)).strip()
        word_confidence = re.search(r"x_wconf\s+(\d+(?:\.\d+)?)", span)
        if raw_text:
            raw_tokens.append(raw_text)
        labels: list[str] = []
        if code == "IOS4-078":
            labels = [label for label in _extract_exhaust_systems(raw_text).split(",") if label]
            compact = re.fullmatch(r"(?:32|52|82)(\d{1,2})", re.sub(r"\D", "", raw_text))
            if compact:
                labels.append(f"V2.{int(compact.group(1))}")
        elif code == "IOS4-079":
            label = _normalize_supply_token(raw_text)
            if label:
                labels.append(label)
        if not labels:
            continue
        x0, y0, x1, y1 = (int(value) / 2.0 for value in coords.groups())
        box = [clip_rect.x0 + x0, clip_rect.y0 + y0, clip_rect.x0 + x1, clip_rect.y0 + y1]
        for label in dict.fromkeys(labels):
            word = {"text": label, "bbox": box, "ocr_source_text": raw_text}
            if word_confidence:
                word["confidence"] = float(word_confidence.group(1)) / 100.0   # Tesseract x_wconf, measured
            words.append(word)
    return words, " ".join(raw_tokens)


def _is_relevant_pd_ventilation_sheet(code: str, snapshot: dict[str, Any], document=None) -> bool:
    return classify_ventilation_sheet(snapshot, document) is not None


def _is_rd_ventilation_drawing_document(code: str, document: DocumentVersion, snapshot: dict[str, Any] | None = None) -> bool:
    if registry_excludes_ventilation(document):
        return False
    if snapshot is None:
        return bool(document_discovery_hints(document))
    return classify_ventilation_sheet(snapshot, document) is not None


def _extract_warm_floor_observations(
    documents: dict[int, DocumentVersion],
    snapshots: dict[tuple[int, int], dict[str, Any]],
    identity_report: dict[str, Any] | None = None,
) -> tuple[list[RuleObservation], dict[str, Any]]:
    report = identity_report if identity_report is not None else new_room_identity_report()
    pd_rows: list[dict[str, Any]] = []
    rd_rows: list[dict[str, Any]] = []
    rd_zone_rows: list[dict[str, Any]] = []
    owner: dict[int, DocumentVersion] = {}
    for (document_id, _page_number), snapshot in sorted(snapshots.items()):
        document = documents[document_id]
        stage = effective_dataset_stage(document)
        if stage not in {"PD", "RD"} or not classify_ventilation_sheet(snapshot, document):
            continue
        rows = warm_floor_rows(snapshot)
        for row in rows:
            row["sheet"] = str(document_id)
            owner[id(row)] = document
        (pd_rows if stage == "PD" else rd_rows).extend(rows)
        if stage == "RD":
            for zone in room_zone_index(snapshot):
                zone["sheet"] = str(document_id)
                owner[id(zone)] = document
                rd_zone_rows.append(zone)

    pd_groups = group_by_identity(pd_rows)
    labels = _location_labels(group["key"] for group in pd_groups)
    observations: list[RuleObservation] = []
    report_rows = []
    project_rooms = 0
    for group in pd_groups:
        location = labels[group["key"]]
        if group["conflict"]:
            report_rows.append({"location": location, "status": "CONFLICT", "reason": "DUPLICATE_ROOM_KEY"})
            continue
        project_row = group["row"]
        project_document = owner[id(project_row)]
        project_rooms += 1
        pd_snapshot = project_row["snapshot"]
        observations.append(_ventilation_observation(
            "FREE_SEARCH",
            {**project_row, "location": location, "value": "Контур теплого пола показан в ПД",
             "normalized_value": "WARM_FLOOR"},
            project_document, pd_snapshot, "PD", None, extractor="room_drawing_compare:warm-floor",
        ))
        worked = match_room_rows([project_row], rd_rows)[0]
        if worked["kind"] in {"CONFLICT", "AMBIGUOUS"}:
            report_rows.append({"location": location, "status": worked["kind"], "reason": worked["reason"]})
            continue
        if worked["kind"] == "MATCHED":
            actual_row = worked["actual"]
            actual_document = owner[id(actual_row)]
            actual_snapshot = actual_row["snapshot"]
            actual_value, actual_norm, actual_bbox = "Контур теплого пола показан в РД", "WARM_FLOOR", actual_row["bbox_pdf"]
            comparison = "PRESENT_IN_BOTH"
        else:
            zone_match = match_room_rows([project_row], rd_zone_rows)[0]
            if zone_match["kind"] in {"CONFLICT", "AMBIGUOUS"}:
                report_rows.append({"location": location, "status": zone_match["kind"], "reason": zone_match["reason"]})
                continue
            if zone_match["kind"] != "MATCHED":
                report_rows.append({"location": location, "status": "NO_RD_ROOM_ZONE"})
                continue
            actual_row = zone_match["actual"]
            actual_document = owner[id(actual_row)]
            actual_snapshot = actual_row["snapshot"]
            actual_value, actual_norm, actual_bbox = "Контур теплого пола не распознан в зоне РД", "MISSING_WARM_FLOOR", actual_row["bbox_pdf"]
            comparison = "ROOM_FOUND_FLOOR_MARKER_NOT_FOUND"
        observations.append(_ventilation_observation(
            "FREE_SEARCH",
            {"location": location, "value": actual_value, "normalized_value": actual_norm,
             "bbox_pdf": actual_bbox,
             "confidence": actual_row.get("confidence") if actual_row.get("confidence") is not None else UNMEASURED_CONFIDENCE,
             "text_confidence": actual_row.get("text_confidence"), "link_confidence": actual_row.get("link_confidence"),
             "context": f"Помещение {location}; статус распознавания контура теплого пола: {comparison}"},
            actual_document, actual_snapshot, "RD", None, extractor="room_drawing_compare:warm-floor",
        ))
        report_rows.append({
            "location": location, "status": comparison, "match_basis": worked.get("match_basis"),
            "PD": {"file_id": getattr(project_document, "dataset_file_id", None), "page": int(pd_snapshot["page"]),
                   "bbox_pdf": project_row["bbox_pdf"]},
            "RD": {"file_id": getattr(actual_document, "dataset_file_id", None), "page": int(actual_snapshot["page"]),
                   "bbox_pdf": actual_bbox},
        })
    return observations, {
        "code": "FREE_SEARCH", "project_rooms": project_rooms,
        "compared_rooms": sum(row.get("status") in {"PRESENT_IN_BOTH", "ROOM_FOUND_FLOOR_MARKER_NOT_FOUND"} for row in report_rows),
        "rooms": report_rows,
    }


def _pd_exhaust_room_links(snapshot: dict[str, Any]) -> dict[str, Any]:
    links = room_system_links(snapshot, kinds={"branch"})
    rows = []
    for row in links["rows"]:
        labels = row["systems"]
        rows.append({**row, "value": _ventilation_value("IOS4-078", ",".join(labels)), "snapshot": snapshot})
    return {**links, "rows": rows}


def _pd_exhaust_room_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return _pd_exhaust_room_links(snapshot)["rows"]


def _pd_supply_room_links(snapshot: dict[str, Any]) -> dict[str, Any]:
    words = snapshot.get("words") or []
    width, height = float(snapshot["width"]), float(snapshot["height"])
    context = detect_sheet_context(snapshot)
    accepted = extract_room_words(snapshot)
    rooms = sorted(accepted, key=lambda room: (room["bbox"][1], room["bbox"][0], room["room"]))
    vent_rooms = [word for word in words if "венткамер" in str(word.get("text") or "").lower()]
    supply = []
    for word in words:
        token = _normalize_supply_token(str(word.get("text") or ""))
        if token:
            supply.append((token, word))
    output = []
    ambiguous = []
    for vent_room in vent_rooms:
        linked_rooms = sorted(
            ((room, _normalized_distance(vent_room["bbox"], room["bbox"], width, height)) for room in rooms),
            key=lambda item: (item[1], item[0]["bbox"][1], item[0]["bbox"][0], item[0]["room"]),
        )
        if not linked_rooms or linked_rooms[0][1] > 0.06:
            continue
        room, room_distance = linked_rooms[0]
        # The ventilation-chamber caption must be clearly nearer to one room number
        # than to the next one, otherwise the chamber's room number is not known.
        if len(linked_rooms) > 1 and linked_rooms[1][1] <= 0.06 and (
                linked_rooms[1][1] - room_distance) <= AMBIGUITY_MARGIN * linked_rooms[1][1]:
            ambiguous.append({
                "label": "ВЕНТКАМЕРА", "kind": "vent_chamber", "bbox": vent_room["bbox"],
                "text_confidence": None, "reason": "AMBIGUOUS_ROOM_LABEL",
                "candidates": [{"location": candidate["room"], "bbox": candidate["bbox"],
                                "distance": round(distance, 4)}
                               for candidate, distance in linked_rooms if distance <= 0.06
                               and (distance - room_distance) <= AMBIGUITY_MARGIN * distance],
            })
            continue
        nearby = sorted(
            ((token, word, _normalized_distance(word["bbox"], room["bbox"], width, height))
             for token, word in supply),
            key=lambda item: (item[2], item[0]),
        )
        if not nearby or nearby[0][2] > 0.08:
            continue
        token, system_word, system_distance = nearby[0]
        runner_up = nearby[1][2] if len(nearby) > 1 and nearby[1][0] != token else None
        link_confidence = max(
            0.05, min(0.95, round(0.5 * (1.0 - room_distance / 0.06) + 0.5 * (1.0 - system_distance / 0.08), 3)))
        text_confidence = _min_known_confidence(room["word"], vent_room, system_word)
        bbox = _union_bbox([room["bbox"], vent_room["bbox"], system_word["bbox"]])
        output.append({
            "location": room["room"],
            "value": _ventilation_value("IOS4-079", token),
            "normalized_value": token,
            "systems": [token],
            "bbox_pdf": bbox,
            "room_bbox": room["bbox"],
            "text_confidence": text_confidence, "link_confidence": link_confidence,
            "confidence": min(v for v in (text_confidence, link_confidence) if v is not None),
            "linkage": "vent_chamber_caption", "wall_check": WALL_CHECK_STATUS,
            "floor": context["floor"], "building": context["building"],
            "floor_source": context["floor_source"], "floor_status": context["floor_status"],
            "sheet": snapshot.get("sheet") or "", "sheet_page": snapshot.get("page"),
            "context": f"Помещение {room['room']} (венткамера); проектная приточная установка: {token}",
            "snapshot": snapshot,
        })
    return {"rows": output, "ambiguous": ambiguous, "sheet": context, "wall_check": WALL_CHECK_STATUS,
            "excluded_zone_rooms": excluded_zone_rooms(snapshot)}


def _pd_supply_room_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return _pd_supply_room_links(snapshot)["rows"]


def _min_known_confidence(*words: dict[str, Any]) -> float | None:
    known = []
    for word in words:
        value = (word or {}).get("confidence")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            known.append(float(value) / 100.0 if float(value) > 1.0 else float(value))
    return min(known) if known else None


def _extract_exhaust_systems(text: str) -> str:
    normalized = str(text).upper().replace(",", ".")
    systems = set()
    pattern = r"(?<![A-ZА-Я0-9])(?P<prefix>V|B|В|8)?\s*(?P<system>\d{1,2})\s*[.\s_-]+\s*(?P<branch>\d{1,2})(?!\d)"
    for match in re.finditer(pattern, normalized):
        system = int(match.group("system"))
        if not match.group("prefix") and system in {3, 5, 8, 32, 52, 82}:
            # Common OCR substitution in a printed Cyrillic В2 branch mark.
            system = 2
        systems.add(f"V{system}.{int(match.group('branch'))}")
    # A room may be labelled with a changed system trunk (e.g. В3) where
    # the project drawing specifies branches В3.1 and В3.2. Preserve that
    # system-level value so comparison can distinguish it from no label.
    for match in re.finditer(r"(?<![A-ZА-Я0-9])[VВB]\s*(\d{1,2})(?![.\d])", normalized):
        systems.add(f"V{int(match.group(1))}")
    return ",".join(sorted(systems, key=_natural_system_key))


def _extract_supply_configuration(text: str) -> str:
    normalized = str(text).upper().replace(",", ".")
    systems = {
        f"P{int(match.group(1))}" + (f".{int(match.group(2))}" if match.group(2) else "")
        for match in re.finditer(r"(?<![A-ZА-Я0-9])(?:P|Р|П)\s*(\d{1,2})(?:[.]\s*(\d{1,2}))?(?!\d)", normalized)
    }
    dimensions = {
        f"{int(match.group(1))}x{int(match.group(2))}"
        for match in re.finditer(r"(?<!\d)(\d{2,4})\s*[XХ×]\s*(\d{2,4})(?!\d)", normalized)
    }
    parts = sorted(systems, key=_natural_system_key) + sorted(dimensions, key=_dimension_key)
    return ",".join(parts)


def _is_exhaust_violation(expected: str, actual: str) -> bool | None:
    expected_systems = _exhaust_system_set(expected)
    if not expected_systems:
        return None
    if str(actual) == _MISSING_EXHAUST:
        return True
    actual_systems = _exhaust_system_set(actual)
    if not actual_systems:
        return None
    missing = expected_systems - actual_systems
    if not missing:
        return False
    expected_trunks = {system.split(".", 1)[0] for system in expected_systems}
    if any(system in expected_trunks for system in actual_systems):
        return True
    if len(expected_systems) == len(actual_systems) == 1 and _single_system_ocr_alias(expected_systems, actual_systems):
        return False
    if not (expected_systems & actual_systems):
        return len(actual_systems) >= 2
    return (len(missing) / len(expected_systems)) >= (1 / 3)


def _is_supply_violation(expected: str, actual: str) -> bool | None:
    expected_systems = _supply_system_set(expected)
    if not expected_systems:
        return None
    actual_systems = _supply_system_set(actual)
    if actual_systems:
        return expected_systems != actual_systems
    dimensions = _dimension_set(actual)
    if dimensions:
        return len(dimensions) >= _SUPPLY_DIMENSION_ONLY_TRIGGER_COUNT
    return None


def _exhaust_system_set(value: str) -> set[str]:
    return {
        f"V{int(match.group(1))}" + (f".{int(match.group(2))}" if match.group(2) else "")
        for match in re.finditer(r"V(\d{1,2})(?:[.](\d{1,2}))?(?!\d)", str(value).upper())
    }


def _supply_system_set(value: str) -> set[str]:
    return {
        f"P{int(match.group(1))}" + (f".{int(match.group(2))}" if match.group(2) else "")
        for match in re.finditer(r"P(\d{1,2})(?:[.](\d{1,2}))?(?!\d)", str(value).upper())
    }


def _dimension_set(value: str) -> set[str]:
    return {
        f"{int(match.group(1))}x{int(match.group(2))}"
        for match in re.finditer(r"(?<!\d)(\d{2,4})x(\d{2,4})(?!\d)", str(value).lower())
    }


def _single_system_ocr_alias(expected_systems: set[str], actual_systems: set[str]) -> bool:
    expected = next(iter(expected_systems))
    actual = next(iter(actual_systems))
    return expected.endswith("0") and expected[:-1] == actual


def _ventilation_observation(code, row, document, snapshot, stage, source_fragment, *, extractor):
    return RuleObservation(
        parameter_code=code,
        location=row["location"],
        stage=stage,
        value=row["value"],
        normalized_value=row["normalized_value"],
        confidence=row["confidence"],
        document=document,
        page=int(snapshot["page"]),
        bbox_normalized=_normalized_bbox(row["bbox_pdf"], snapshot["width"], snapshot["height"]),
        bbox_pdf=row["bbox_pdf"],
        page_width=float(snapshot["width"]),
        page_height=float(snapshot["height"]),
        extractor=extractor,
        context=row["context"],
        source_fragment=source_fragment,
        text_confidence=row.get("text_confidence"),
        link_confidence=row.get("link_confidence"),
    )


def _drawing_room_words(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return extract_room_words(snapshot)


def _best_fragment(rows: list[SourceFragment]) -> SourceFragment | None:
    return sorted(rows, key=lambda row: (-(row.confidence or 0), row.id or 0))[0] if rows else None


def _centered_clip(bbox, width, height, half_width, half_height) -> list[float]:
    center_x, center_y = _bbox_center(bbox)
    return [
        max(0.0, center_x - half_width),
        max(0.0, center_y - half_height),
        min(float(width), center_x + half_width),
        min(float(height), center_y + half_height),
    ]


def _text_in_bbox(snapshot: dict[str, Any], bbox: list[float]) -> str:
    x1, y1, x2, y2 = bbox
    return " ".join(
        str(word["text"])
        for word in snapshot.get("words") or []
        if x1 <= _bbox_center(word["bbox"])[0] <= x2 and y1 <= _bbox_center(word["bbox"])[1] <= y2
    )


def _bbox_center(bbox) -> tuple[float, float]:
    return ((float(bbox[0]) + float(bbox[2])) / 2, (float(bbox[1]) + float(bbox[3])) / 2)


def _normalized_distance(left, right, width, height) -> float:
    left_x, left_y = _bbox_center(left)
    right_x, right_y = _bbox_center(right)
    return (((left_x - right_x) / width) ** 2 + ((left_y - right_y) / height) ** 2) ** 0.5


def _normalize_exhaust_token(value: str) -> str | None:
    return _extract_exhaust_systems(value) or None


def _normalize_supply_token(value: str) -> str | None:
    match = re.fullmatch(r"[PРП]\s*(\d{1,2})(?:[.,](\d{1,2}))?", value.strip(), re.IGNORECASE)
    if not match:
        return None
    return f"P{int(match.group(1))}" + (f".{int(match.group(2))}" if match.group(2) else "")


def _natural_system_key(value: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _dimension_key(value: str) -> tuple[int, int]:
    left, right = value.split("x", 1)
    return int(left), int(right)


def _ventilation_value(code: str, normalized: str) -> str:
    label = "Системы" if code == "IOS4-078" else "Конфигурация"
    return f"{label}: {normalized.replace(',', ', ')}"


def _extract_absolute_zero(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(snapshot.get("text") or "")
    # The extraction patterns below are the semantic anchor for this value;
    # unlike a literal vocabulary check, they accept normal Russian case
    # inflections such as "абсолютной отметке".
    patterns = (
        r"(?:относительн\w*\s+отметк\w*|отметк\w*\s+нуля).{0,120}?(?:абсолютн\w*\s+отметк\w*|абс\.?\s*отм\.?)[^0-9+]{0,30}\+?(?P<value>\d{3}[.,]\d{2,3})",
        r"(?:абсолютн\w*\s+отметк\w*|абс\.?\s*отм\.?)[^0-9+]{0,30}\+?(?P<value>\d{3}[.,]\d{2,3})",
    )
    match = next((match for pattern in patterns if (match := re.search(pattern, text, re.IGNORECASE))), None)
    if not match:
        return []
    value = match.group("value").replace(",", ".")
    bbox = _bbox_for_tokens(snapshot["words"], [match.group("value")])
    if not bbox:
        return []
    return [{
        "location": "Отметка 0.000",
        "value": value,
        "normalized_value": _decimal_text(value),
        "bbox_pdf": bbox,
        "confidence": 0.92,
        "context": _match_context(text, match.start(), match.end()),
    }]


def _extract_concrete_classes(
    snapshot: dict[str, Any],
    page_fragments: Iterable[SourceFragment],
    *,
    document: DocumentVersion | None = None,
) -> list[dict[str, Any]]:
    text, words, offsets = _indexed_words(snapshot.get("words") or [])
    # Candidate-page selection is vocabulary-driven; organizer field codes do
    # not establish what an element on the source page is called.
    del page_fragments
    out = []
    document_location: str | None = None
    document_location_checked = False
    for match in re.finditer(r"(?<![A-Za-zА-Яа-я0-9])(?:B|В)\s*(?P<value>\d{2}(?:[.,]\d)?)(?!\d)", text, re.IGNORECASE):
        index = _word_index_at(offsets, match.start("value"))
        if index is None:
            continue
        nearby = " ".join(word["text"] for word in words[max(0, index - 35): index]).lower()
        location = _concrete_location(nearby)
        local_context = " ".join(word["text"] for word in words[max(0, index - 22): index + 14])
        if any(token in local_context.lower() for token in ("бетонная подготов", "тощего бетон", "подбетон")):
            continue
        excluded_fallback = any(token in local_context.lower() for token in ("лестниц", "форшах", "сваи", "свай"))
        fallback = (
            not location and _rule_anchor_present("KR-058", text)
            and any(token in text.lower() for token in ("фунд", "ростверк"))
            and not excluded_fallback
        )
        if fallback:
            location = _foundation_element_name(text)
        if not location and document is not None and not excluded_fallback:
            # This page (e.g. a materials-quality registry attached to a hidden-works
            # act) states the concrete class without repeating the element name; the
            # act's own standardized "permitted next work" field, read once per
            # document, supplies it. See `_act_subject_location`.
            if not document_location_checked:
                document_location = _act_subject_location(document)
                document_location_checked = True
            if document_location:
                location = document_location
                fallback = True
        if not location:
            continue
        raw = match.group("value").replace(",", ".")
        value = f"B{raw}"
        out.append({
            "location": location,
            "value": value,
            "normalized_value": value,
            "bbox_pdf": list(words[index]["bbox"]),
            "confidence": 0.75 if fallback else 0.9,
            "context": local_context,
        })
    return out


_ACT_HIDDEN_WORKS_MARKER = re.compile(r"акт\w*\s+освидетельствовани\w*\s+скрыт\w*\s+работ", re.IGNORECASE)
_ACT_NEXT_WORK_MARKER = re.compile(r"разрешается\s+производство\s+последующих\s+работ", re.IGNORECASE)
_ACT_SUBJECT_WINDOW_CHARS = 160


def _act_subject_location(document: DocumentVersion) -> str | None:
    """Акт освидетельствования скрытых работ (a standardized hidden-works
    inspection act form) states, in its own fixed field 7 ("Разрешается
    производство последующих работ"), the one structural element the whole
    act concerns. A materials/protocol registry attached later in the same
    act lists class codes (e.g. concrete class) without repeating that
    element name anywhere nearby, so the per-page nearby-word window in
    `_extract_concrete_classes` never resolves a location there. This reads
    the act's own declared subject once per document from its first pages —
    generic to the standard AOSR form, not tied to any one document/object.
    """
    try:
        pages = extract_original_pages(document, (1, 2, 3))
    except (FileNotFoundError, ValueError, RuntimeError):
        return None
    if not pages:
        return None
    combined = " ".join(str(pages[page]["text"]) for page in sorted(pages) if pages.get(page))
    if not _ACT_HIDDEN_WORKS_MARKER.search(combined):
        return None
    marker = _ACT_NEXT_WORK_MARKER.search(combined)
    if not marker:
        return None
    window = combined[marker.end(): marker.end() + _ACT_SUBJECT_WINDOW_CHARS].lower()
    return _concrete_location(window)


_SLAB_MENTION_PATTERN = re.compile(
    r"фундамент\w*.{0,60}?плит\w*|плит\w*.{0,60}?фундамент\w*|ростверк\w*",
    re.IGNORECASE,
)
# "h=1200 мм" is a standard structural-drawing shorthand for element thickness,
# used interchangeably with the word "толщина" — not specific to any one document.
_THICKNESS_ANCHOR_PATTERN = re.compile(r"толщин\w*|(?<![a-zа-я])h\s*=", re.IGNORECASE)
_THICKNESS_ANCHOR_CONTEXT_CHARS = 220


def _extract_foundation_thickness(snapshot: dict[str, Any], *, stage: str | None = None) -> list[dict[str, Any]]:
    text, words, offsets = _indexed_words(snapshot.get("words") or [])
    # Find slab mentions first (order-agnostic, tolerant of words in between), then
    # look for a thickness anchor anywhere in a wider local window around each one,
    # instead of requiring "толщина" and "фундамент"/"плита" inside one fixed-order
    # regex. This tolerates the anchor terms appearing in a different order, further
    # apart, or across what was a line break in the source drawing (line breaks do
    # not survive `_indexed_words`, which already joins tokens with single spaces).
    candidates = []
    for slab in _SLAB_MENTION_PATTERN.finditer(text):
        local_start = max(0, slab.start() - _THICKNESS_ANCHOR_CONTEXT_CHARS)
        local_end = min(len(text), slab.end() + _THICKNESS_ANCHOR_CONTEXT_CHARS)
        anchor = _THICKNESS_ANCHOR_PATTERN.search(text, local_start, local_end)
        if not anchor:
            continue
        window_start = anchor.start()
        window_end = min(len(text), anchor.end() + 150)
        window = text[window_start:window_end]
        values = []
        positions = []
        for match in re.finditer(r"(?<!\d)(\d{3,4})(?!\d)", window):
            value = int(match.group(1))
            unit_context = window[max(0, match.start(1) - 3): min(len(window), match.end(1) + 14)].lower()
            if 500 <= value <= 3000 and "мм" in unit_context and value not in values:
                values.append(value)
                positions.append(window_start + match.start(1))
            if len(values) == 2:
                break
        if values:
            candidates.append((values, positions, window_start, window_end, slab.group(0)))
    if not candidates:
        return []
    values, positions, start, end, element_text = max(candidates, key=lambda item: len(item[0]))
    indices = [index for position in positions if (index := _word_index_at(offsets, position)) is not None]
    bbox = _union_bbox([words[index]["bbox"] for index in indices])
    if not bbox:
        return []
    normalized = "/".join(str(value) for value in sorted(values))
    return [{
        "location": _foundation_element_name(element_text),
        "value": f"{normalized} мм",
        "normalized_value": normalized,
        "bbox_pdf": bbox,
        "confidence": 0.9 if len(values) == 2 else 0.82,
        "context": text[start:end],
    }]


def _foundation_element_name(source_text: str) -> str:
    """Normalize the element noun actually found in the document text."""
    return "Ростверк" if re.search(r"ростверк", str(source_text), re.IGNORECASE) else "Фундаментная плита"


_RULE_PARAMETER_NAMES = {
    "PZ-009": "Абсолютная отметка 0.000",
    "KR-055": "Класс прочности бетона монолитных конструкций",
    "KR-058": "Толщина монолитной фундаментной плиты / ростверка",
}


def _rule_anchor_present(code: str, text: str, *, stage: str | None = None) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).casefold().replace("ё", "е")
    phrases = anchor_phrases(code, _RULE_PARAMETER_NAMES.get(code), stage)
    return any(
        re.sub(r"\s+", " ", phrase).casefold().replace("ё", "е") in normalized
        for phrase in phrases if phrase
    )


def _concrete_location(context: str) -> str | None:
    if re.search(r"кроме\s+фундамент\w*\s+плит", context):
        return "Вертикальные конструкции подземной части"
    anchors = {}
    if "грунт" in context and "стен" in context:
        anchors["Стена в грунте"] = max(context.rfind("грунт"), context.rfind("стен"))
    if "фундамент" in context and "плит" in context:
        anchors["Фундаментная плита"] = max(context.rfind("фундамент"), context.rfind("плит"))
    vertical = max(context.rfind(token) for token in ("пилон", "колонн", "вертикальн", "стен"))
    if vertical >= 0:
        anchors["Вертикальные конструкции подземной части"] = vertical
    return max(anchors, key=anchors.get) if anchors else None


def _indexed_words(raw_words: Iterable[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[tuple[int, int]]]:
    words = [word for word in raw_words if str(word.get("text") or "").strip()]
    parts, offsets, cursor = [], [], 0
    for word in words:
        token = str(word["text"])
        if parts:
            cursor += 1
        start = cursor
        parts.append(token)
        cursor += len(token)
        offsets.append((start, cursor))
    return " ".join(parts), words, offsets


def _word_index_at(offsets: list[tuple[int, int]], position: int) -> int | None:
    for index, (start, end) in enumerate(offsets):
        if start <= position <= end:
            return index
    return None


def _bbox_for_tokens(words: Iterable[dict[str, Any]], tokens: Iterable[str]) -> list[float] | None:
    wanted = [str(token).replace(",", ".").lstrip("+") for token in tokens]
    boxes = []
    for token in wanted:
        # PDF word extraction attaches trailing sentence punctuation to the last
        # token (e.g. a value at the end of a sentence comes through as "159.95."),
        # so strip it before comparing rather than requiring a byte-exact match.
        match = next(
            (
                word for word in words
                if str(word.get("text") or "").replace(",", ".").lstrip("+").rstrip(".,;:") == token
            ),
            None,
        )
        if match:
            boxes.append(match["bbox"])
    return _union_bbox(boxes)


def _union_bbox(boxes: Iterable[Iterable[float]]) -> list[float] | None:
    rows = [list(map(float, box)) for box in boxes if box and len(box) == 4]
    if not rows:
        return None
    return [min(row[0] for row in rows), min(row[1] for row in rows), max(row[2] for row in rows), max(row[3] for row in rows)]


def _normalized_bbox(bbox: list[float], width: float, height: float) -> list[float]:
    return [round(bbox[0] / width, 6), round(bbox[1] / height, 6), round(bbox[2] / width, 6), round(bbox[3] / height, 6)]


def _match_context(text: str, start: int, end: int, margin: int = 180) -> str:
    return text[max(0, start - margin): min(len(text), end + margin)]


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        return None


def _decimal_text(value: str) -> str:
    parsed = _decimal(value)
    if parsed is None:
        return str(value)
    return format(parsed.normalize(), "f")


def _number_list(value: str) -> list[Decimal]:
    return [parsed for token in re.findall(r"\d+(?:[.,]\d+)?", str(value)) if (parsed := _decimal(token)) is not None]


def _deduplicate(rows: Iterable[RuleObservation]) -> list[RuleObservation]:
    best: dict[tuple, RuleObservation] = {}
    for row in rows:
        key = (row.parameter_code, row.location, row.stage, row.normalized_value, int(row.document.id), row.page)
        if key not in best or row.confidence > best[key].confidence:
            best[key] = row
    deduplicated = list(best.values())
    return sorted(
        _suppress_duplicate_exhaust_findings(deduplicated),
        key=lambda row: (row.parameter_code, row.location, row.stage, -row.confidence, document_order_key(row.document), row.page),
    )


def _suppress_duplicate_exhaust_findings(rows: list[RuleObservation]) -> list[RuleObservation]:
    by_location: dict[str, dict[str, RuleObservation]] = {}
    for row in rows:
        if row.parameter_code != "IOS4-078" or row.stage not in {"PD", "RD"}:
            continue
        current = by_location.setdefault(row.location, {}).get(row.stage)
        if current is None or row.confidence > current.confidence:
            by_location[row.location][row.stage] = row

    candidates = []
    for location, stages in by_location.items():
        expected, actual = stages.get("PD"), stages.get("RD")
        if not expected or not actual:
            continue
        if _is_exhaust_violation(expected.normalized_value, actual.normalized_value) is not True:
            continue
        candidates.append({
            "location": location,
            "expected": expected,
            "actual": actual,
            "expected_set": _exhaust_system_set(expected.normalized_value),
            "actual_set": _exhaust_system_set(actual.normalized_value),
        })

    suppressed: set[tuple[str, int, int]] = set()
    for index, left in enumerate(candidates):
        for right in candidates[index + 1:]:
            if not _duplicate_exhaust_candidate(left, right):
                continue
            keep = _preferred_exhaust_candidate(left, right)
            loser = right if keep is left else left
            actual = loser["actual"]
            suppressed.add((loser["location"], int(actual.document.id), actual.page))
    if not suppressed:
        return rows
    return [
        row
        for row in rows
        if not (
            row.parameter_code == "IOS4-078"
            and row.stage == "RD"
            and (row.location, int(row.document.id), row.page) in suppressed
        )
    ]


def _duplicate_exhaust_candidate(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_actual, right_actual = left["actual"], right["actual"]
    if int(left_actual.document.id) != int(right_actual.document.id) or left_actual.page != right_actual.page:
        return False
    same_signature = (
        left["expected"].normalized_value == right["expected"].normalized_value
        and left_actual.normalized_value == right_actual.normalized_value
    )
    if same_signature:
        return True
    if _location_distance(left["location"], right["location"]) <= 1:
        return True
    return False


def _preferred_exhaust_candidate(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    same_expected = left["expected"].normalized_value == right["expected"].normalized_value
    left_missing = left["actual"].normalized_value == _MISSING_EXHAUST
    right_missing = right["actual"].normalized_value == _MISSING_EXHAUST
    if same_expected and left_missing != right_missing:
        return right if left_missing else left
    if same_expected and left["actual"].normalized_value == right["actual"].normalized_value:
        if left_missing:
            return max((left, right), key=lambda item: (_bbox_top(item["actual"]), _location_number(item["location"])))
        return min((left, right), key=lambda item: (_bbox_top(item["actual"]), -_location_number(item["location"])))
    left_score = _exhaust_candidate_score(left)
    right_score = _exhaust_candidate_score(right)
    if left_score != right_score:
        return left if left_score > right_score else right
    return min((left, right), key=lambda item: (_bbox_top(item["actual"]), -_location_number(item["location"])))


def _exhaust_candidate_score(candidate: dict[str, Any]) -> tuple[float, int, float]:
    expected = candidate["expected_set"]
    actual = candidate["actual_set"]
    if not expected:
        missing_ratio = 0.0
    elif candidate["actual"].normalized_value == _MISSING_EXHAUST:
        missing_ratio = 1.0
    else:
        missing_ratio = len(expected - actual) / len(expected)
    non_missing_actual = int(candidate["actual"].normalized_value != _MISSING_EXHAUST)
    return (missing_ratio, non_missing_actual, float(candidate["actual"].confidence or 0))


def _bbox_iou(left: list[float], right: list[float]) -> float:
    try:
        lx1, ly1, lx2, ly2 = map(float, left)
        rx1, ry1, rx2, ry2 = map(float, right)
    except (TypeError, ValueError):
        return 0.0
    ix1, iy1 = max(lx1, rx1), max(ly1, ry1)
    ix2, iy2 = min(lx2, rx2), min(ly2, ry2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def _bbox_top(row: RuleObservation) -> float:
    return float(row.bbox_normalized[1]) if row.bbox_normalized and len(row.bbox_normalized) == 4 else 1.0


def _location_number(location: str) -> int:
    return int(location) if str(location).isdigit() else -1


def _location_distance(left: str, right: str) -> int:
    if not str(left).isdigit() or not str(right).isdigit():
        return 10_000
    return abs(int(left) - int(right))
