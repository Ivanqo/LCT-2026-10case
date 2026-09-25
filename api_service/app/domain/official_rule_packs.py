"""Conservative value extraction for the first official CASE10 rule packs.

Only machine-generated locator annotations without check_id are used to select
pages. Values are extracted again from the original, hash-verified PDFs.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
import time
from typing import Any, Iterable

from ..config import settings
from ..db.models import DocumentVersion, SourceFragment
from .dataset_sources import document_page_count_hint, extract_original_pages, ocr_original_clip, ocr_page_snapshot


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


# See official_evidence.py's identically-named `_INFERENCE_SOURCE_SYSTEMS`
# for why both source systems are accepted here (organizer-supplied
# `learning_annotation` training data and this project's own
# `live_candidate_tagger.py` live-scanned equivalent).
_INFERENCE_SOURCE_SYSTEMS = frozenset({"learning_annotation", "live_tagger"})


def is_inference_locator(fragment: SourceFragment) -> bool:
    meta = fragment.metadata_json or {}
    return (
        fragment.source_system in _INFERENCE_SOURCE_SYSTEMS
        and meta.get("annotation_type") == "MATRIX_FIELD"
        and meta.get("status") == "AUTO_FIELD_CANDIDATE"
        and not meta.get("check_id")
    )


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

    for document_id, document in documents.items():
        if str(document.dataset_section or "").upper() != "OV" or str(document.dataset_stage or "").upper() != "RD_ID_MIXED":
            continue
        meta = document.dataset_metadata or {}
        row = meta.get("document_manifest") or meta.get("files_index") or {}
        page_count = int(row.get("pdf_pages") or row.get("source_page_count") or 40)
        for code in ("IOS4-078", "IOS4-079"):
            if code not in active_codes:
                continue
            existing = candidates.get((document_id, code), {})
            candidates[(document_id, code)] = {
                page: existing.get(page, []) for page in range(1, min(page_count, 40) + 1)
            }

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

    for code in ("IOS4-078", "IOS4-079"):
        if code not in active_codes:
            continue
        output[code].extend(_extract_ventilation_observations(code, documents, candidates, snapshots))

    for code, observations in output.items():
        output[code] = _deduplicate(observations)
    return output, {"documents": documents, "scanned_pages": set(snapshots.keys())}


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
            key=lambda doc: int(doc.id),
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
        rows = _extract_foundation_thickness(snapshot)
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


def _extract_ventilation_observations(
    code: str,
    documents: dict[int, DocumentVersion],
    candidates: dict[tuple[int, str], dict[int, list[SourceFragment]]],
    snapshots: dict[tuple[int, int], dict],
) -> list[RuleObservation]:
    expected_rows: dict[str, tuple[dict[str, Any], DocumentVersion, SourceFragment | None]] = {}
    for (document_id, candidate_code), pages in candidates.items():
        document = documents[document_id]
        if candidate_code != code or effective_dataset_stage(document) != "PD":
            continue
        for page_number in _prioritized_pages(pages, limit=220):
            snapshot = snapshots.get((document_id, page_number))
            if not snapshot or not _is_relevant_pd_ventilation_sheet(code, snapshot):
                continue
            rows = _pd_exhaust_room_rows(snapshot) if code == "IOS4-078" else _pd_supply_room_rows(snapshot)
            for row in rows:
                if code == "IOS4-078" and not _is_supported_exhaust_pd_row(row):
                    continue
                source_fragment = _best_fragment(pages.get(page_number, []))
                previous = expected_rows.get(row["location"])
                if previous is None or row["confidence"] > previous[0]["confidence"]:
                    expected_rows[row["location"]] = (row, document, source_fragment)

    if not expected_rows:
        return []

    rd_pages = []
    expected_locations = set(expected_rows)
    for (document_id, page_number), snapshot in snapshots.items():
        document = documents[document_id]
        if not _is_rd_ventilation_drawing_document(code, document):
            continue
        room_words = _drawing_room_words(snapshot)
        locations = {room["room"] for room in room_words}
        overlap = locations & expected_locations
        if not overlap:
            continue
        text = str(snapshot.get("text") or "").lower()
        title_score = sum(token in text for token in ("план", "вентиляц", "воздуховод", "ов1"))
        score = len(overlap) * 100 + title_score * 10 + min(len(locations), 50)
        rd_pages.append((score, document, snapshot, room_words))

    observations = []
    for location, (row, document, source_fragment) in expected_rows.items():
        observations.append(_ventilation_observation(
            code, row, document, row["snapshot"], "PD", source_fragment,
            extractor=f"official_rule:{code.lower()}-layout",
        ))
        matches = [item for item in rd_pages if location in {word["room"] for word in item[3]}]
        if not matches:
            continue
        _score, rd_document, rd_snapshot, room_words = max(
            matches,
            key=lambda item: (item[0], float(item[2]["width"]) * float(item[2]["height"]), -int(item[2]["page"])),
        )
        room_word = min(
            (word for word in room_words if word["room"] == location),
            key=lambda word: (word["bbox"][1], word["bbox"][0]),
        )
        half_width, half_height = ((180.0, 160.0) if code == "IOS4-078" else (260.0, 240.0))
        clip = _centered_clip(room_word["bbox"], rd_snapshot["width"], rd_snapshot["height"], half_width, half_height)
        # Digit/dimension crops (duct sizes like "200x100") are calibrated against
        # English-only OCR; Cyrillic mode adds visually-similar letter/digit
        # confusions here without adding value, since there is no prose to read.
        ocr_text = ocr_original_clip(rd_document, int(rd_snapshot["page"]), clip, lang="eng").strip()
        if not ocr_text:
            # OCR unavailability is an abstention, not proof of missing equipment.
            continue
        clip_text = _text_in_bbox(rd_snapshot, clip)
        evidence_text = "\n".join(part for part in (ocr_text, clip_text) if part)
        normalized = (_extract_exhaust_systems(evidence_text)
                      if code == "IOS4-078" else _extract_supply_configuration(evidence_text))
        if code == "IOS4-078" and not normalized:
            normalized = _MISSING_EXHAUST
            value = "Вытяжная система не распознана в зоне помещения"
            confidence = 0.72
        elif not normalized:
            continue
        else:
            value = _ventilation_value(code, normalized)
            confidence = 0.8
        actual = {
            "location": location,
            "value": value,
            "normalized_value": normalized,
            "bbox_pdf": clip,
            "confidence": confidence,
            "context": f"Лист классифицирован как план ОВ по содержимому. OCR зоны помещения:\n{evidence_text[:1200]}",
        }
        observations.append(_ventilation_observation(
            code, actual, rd_document, rd_snapshot, "RD", None,
            extractor=f"official_rule:{code.lower()}-ocr",
        ))
    return observations


def _is_relevant_pd_ventilation_sheet(code: str, snapshot: dict[str, Any]) -> bool:
    text = str(snapshot.get("text") or "").lower().replace("ё", "е")
    if code == "IOS4-078":
        return "принципиаль" in text and "общеобмен" in text and "вентиляц" in text
    return "теплоснабжен" in text and "приточ" in text and "установ" in text


def _is_rd_ventilation_drawing_document(code: str, document: DocumentVersion) -> bool:
    if str(document.dataset_section or "").upper() != "OV" or str(document.dataset_stage or "").upper() != "RD_ID_MIXED":
        return False
    meta = document.dataset_metadata or {}
    row = meta.get("files_index") or meta.get("document_manifest") or {}
    path = " ".join(str(row.get(key) or "") for key in ("source_relative_path", "relative_path", "output_pdf"))
    normalized = path.upper().replace(" ", "")
    if code in {"IOS4-078", "IOS4-079"}:
        return "РД-ОВ1" in normalized or "RD-OV1" in normalized
    return True


def _pd_exhaust_room_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    words = snapshot.get("words") or []
    width, height = float(snapshot["width"]), float(snapshot["height"])
    systems = []
    for word in words:
        system = _normalize_exhaust_token(str(word.get("text") or ""))
        if system:
            systems.append((system, word))
    output = []
    for room in _drawing_room_words(snapshot):
        room_x, room_y = _bbox_center(room["bbox"])
        nearby = []
        for system, word in systems:
            system_x, system_y = _bbox_center(word["bbox"])
            dx = abs(system_x - room_x) / width
            dy = abs(system_y - room_y) / height
            if dx <= 0.08 and dy <= 0.16:
                nearby.append((system, word, (dx * dx + dy * dy) ** 0.5))
        if not nearby:
            continue
        nearby.sort(key=lambda item: item[2])
        cutoff = min(0.075, nearby[0][2] + 0.025)
        selected = [item for item in nearby if item[2] <= cutoff][:4]
        labels = sorted({item[0] for item in selected}, key=_natural_system_key)
        if not labels:
            continue
        bbox = _union_bbox([room["bbox"], *(item[1]["bbox"] for item in selected)])
        output.append({
            "location": room["room"],
            "value": _ventilation_value("IOS4-078", ",".join(labels)),
            "normalized_value": ",".join(labels),
            "bbox_pdf": bbox,
            "confidence": 0.9,
            "context": f"Помещение {room['room']}; проектные вытяжные системы: {', '.join(labels)}",
            "snapshot": snapshot,
            "linkage": "primary_proximity",
        })
    output.extend(_pd_exhaust_same_row_fallback(snapshot, systems, {row["location"] for row in output}))
    return output


def _pd_exhaust_same_row_fallback(
    snapshot: dict[str, Any],
    systems: list[tuple[str, dict[str, Any]]],
    existing_locations: set[str],
) -> list[dict[str, Any]]:
    width, height = float(snapshot["width"]), float(snapshot["height"])
    output = []
    for room in _drawing_room_words(snapshot):
        location = room["room"]
        if location in existing_locations or not _is_300_room(location):
            continue
        room_x, room_y = _bbox_center(room["bbox"])
        nearby = []
        for system, word in systems:
            system_x, system_y = _bbox_center(word["bbox"])
            dx = abs(system_x - room_x) / width
            dy = abs(system_y - room_y) / height
            if dx <= 0.13 and dy <= 0.055:
                nearby.append((system, word, (dx * dx + dy * dy) ** 0.5))
        if not nearby:
            continue
        nearby.sort(key=lambda item: item[2])
        selected = [item for item in nearby if item[2] <= min(0.13, nearby[0][2] + 0.025)][:4]
        labels = sorted({item[0] for item in selected}, key=_natural_system_key)
        if len(labels) < 2:
            continue
        bbox = _union_bbox([room["bbox"], *(item[1]["bbox"] for item in selected)])
        output.append({
            "location": location,
            "value": _ventilation_value("IOS4-078", ",".join(labels)),
            "normalized_value": ",".join(labels),
            "bbox_pdf": bbox,
            "confidence": 0.84,
            "context": f"Помещение {location}; проектные вытяжные системы по горизонтальной связке: {', '.join(labels)}",
            "snapshot": snapshot,
            "linkage": "same_row_fallback",
        })
    return output


def _pd_supply_room_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    words = snapshot.get("words") or []
    width, height = float(snapshot["width"]), float(snapshot["height"])
    vent_rooms = [word for word in words if "венткамер" in str(word.get("text") or "").lower()]
    supply = []
    for word in words:
        token = _normalize_supply_token(str(word.get("text") or ""))
        if token:
            supply.append((token, word))
    output = []
    for room in _drawing_room_words(snapshot):
        room_x, room_y = _bbox_center(room["bbox"])
        labels = [
            word for word in vent_rooms
            if abs(_bbox_center(word["bbox"])[0] - room_x) <= width * 0.04
            and abs(_bbox_center(word["bbox"])[1] - room_y) <= height * 0.04
        ]
        if not labels:
            continue
        nearby = sorted(
            ((token, word, _normalized_distance(word["bbox"], room["bbox"], width, height))
             for token, word in supply),
            key=lambda item: item[2],
        )
        if not nearby or nearby[0][2] > 0.08:
            continue
        token, system_word, _distance = nearby[0]
        bbox = _union_bbox([room["bbox"], labels[0]["bbox"], system_word["bbox"]])
        output.append({
            "location": room["room"],
            "value": _ventilation_value("IOS4-079", token),
            "normalized_value": token,
            "bbox_pdf": bbox,
            "confidence": 0.88,
            "context": f"Помещение {room['room']} (венткамера); проектная приточная установка: {token}",
            "snapshot": snapshot,
        })
    return output


def _extract_exhaust_systems(text: str) -> str:
    normalized = str(text).upper().replace(",", ".")
    systems = set()
    for match in re.finditer(r"(?<![A-ZА-Я0-9])(?:V|B|В|3|5|8)\s*2\s*[.\s_-]+\s*(\d{1,2})(?!\d)", normalized):
        systems.add(f"V2.{int(match.group(1))}")
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
        f"V2.{int(match.group(1))}"
        for match in re.finditer(r"V2[.](\d{1,2})(?!\d)", str(value).upper())
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


def _is_supported_exhaust_pd_row(row: dict[str, Any]) -> bool:
    location = str(row.get("location") or "")
    if _is_100_room(location):
        return True
    if _is_300_room(location):
        return row.get("linkage") == "same_row_fallback"
    return False


def _is_100_room(location: str) -> bool:
    return location.isdigit() and 100 <= int(location) <= 199


def _is_300_room(location: str) -> bool:
    return location.isdigit() and 300 <= int(location) <= 399


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
    )


def _drawing_room_words(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    height = float(snapshot["height"])
    output = []
    for word in snapshot.get("words") or []:
        token = str(word.get("text") or "").strip(".,;:()")
        bbox = list(map(float, word["bbox"]))
        if re.fullmatch(r"\d{3}", token) and token != "000" and bbox[1] < height * 0.78:
            output.append({"room": token, "bbox": bbox})
    return output


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
    match = re.fullmatch(r"[VВB8]\s*2[.,](\d{1,2})", value.strip(), re.IGNORECASE)
    return f"V2.{int(match.group(1))}" if match else None


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
    page_codes = {str((row.metadata_json or {}).get("code") or "") for row in page_fragments}
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
        fallback = not location and "KR-058" in page_codes and "фунд" in text.lower() and not excluded_fallback
        if fallback:
            location = "Фундаментная плита"
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


_SLAB_MENTION_PATTERN = re.compile(r"фундамент\w*.{0,60}?плит\w*|плит\w*.{0,60}?фундамент\w*", re.IGNORECASE)
# "h=1200 мм" is a standard structural-drawing shorthand for element thickness,
# used interchangeably with the word "толщина" — not specific to any one document.
_THICKNESS_ANCHOR_PATTERN = re.compile(r"толщин\w*|(?<![a-zа-я])h\s*=", re.IGNORECASE)
_THICKNESS_ANCHOR_CONTEXT_CHARS = 220


def _extract_foundation_thickness(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
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
            candidates.append((values, positions, window_start, window_end))
    if not candidates:
        return []
    values, positions, start, end = max(candidates, key=lambda item: len(item[0]))
    indices = [index for position in positions if (index := _word_index_at(offsets, position)) is not None]
    bbox = _union_bbox([words[index]["bbox"] for index in indices])
    if not bbox:
        return []
    normalized = "/".join(str(value) for value in sorted(values))
    return [{
        "location": "Фундаментная плита",
        "value": f"{normalized} мм",
        "normalized_value": normalized,
        "bbox_pdf": bbox,
        "confidence": 0.9 if len(values) == 2 else 0.82,
        "context": text[start:end],
    }]


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
        key=lambda row: (row.parameter_code, row.location, row.stage, -row.confidence, int(row.document.id), row.page),
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
