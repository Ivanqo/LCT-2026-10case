"""Room-local extraction and comparison for HVAC plan and schematic sheets.

Locations remain exactly as printed on the sheet (including leading zeroes).
System labels are normalized only enough to compare common Cyrillic/Latin
letter variants and punctuation used in ventilation drawings.

Room identity (expert review, Step A)
-------------------------------------
A printed room number is not an identifier: the same number repeats on other
floors and in other buildings.  A room is identified by (building, floor,
number).  Building and floor are read from the sheet text (``detect_sheet_context``);
when the floor cannot be established the identity is scoped to the sheet
(document + page), so rooms of different sheets are never merged by number alone.
Two different records with one full key inside one stage are a CONFLICT
(``DUPLICATE_ROOM_KEY``), never a silent overwrite, and the comparison makes
no decision for that key.

System-to-room linkage
----------------------
A system label is linked to the nearest room label inside a window.  When the
second-best room is not clearly farther (relative margin ``AMBIGUITY_MARGIN``)
the label is reported as *ambiguous* with all candidates instead of being
assigned.  ``text_confidence`` (recognition: OCR word confidence, ``None`` for the
exact PDF text layer) and ``link_confidence`` (geometric ranking score, not a
probability) are separate; the legacy ``confidence`` field is the minimum of the
measured ones.  Wall geometry is not available in the page snapshot, so
``wall_check`` is always ``"not_evaluated"``.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable


_ROOM_TOKEN = re.compile(
    r"(?<![\wА-Яа-я])\d{1,4}(?:\.\d{1,3})?(?![\wА-Яа-я])",
    re.IGNORECASE,
)
_SYSTEM_TOKEN = re.compile(
    r"(?<![\wА-Яа-я])(?:[PРП]\s*\d{1,2}(?:[.,]\s*\d{1,2})?|[ВVB]\s*(?:Е|E)|[ВVB]\s*\d{1,2}(?:[.,]\s*\d{1,2})?)(?![\wА-Яа-я])",
    re.IGNORECASE,
)
_FLOOR_MARKER = re.compile(r"тепл\w*\s*пол\w*|контур\w*\s*(?:тепл\w*|отопл\w*)", re.IGNORECASE)
_FLOOR_ABBREVIATION = re.compile(r"(?<![\wА-Яа-я])т\s*[.]?\s*п\s*[.]?(?![\wА-Яа-я])", re.IGNORECASE)
_ROOM_CONTEXT = re.compile(r"(?:пом\w*|помещ\w*|room|№)", re.IGNORECASE)

# Link windows (fractions of the page size).  Kept from the previous version so
# that candidate discovery does not change; only the *decision* among candidates is new.
_PRIMARY_WINDOW = (0.095, 0.17)
_FALLBACK_WINDOW = (0.14, 0.055)
# A label is assigned only if the second-nearest room is at least this much
# farther, relative to the second distance: (d2 - d1) / d2 > AMBIGUITY_MARGIN.
# The label and the room number are two printed points, neither of which marks the
# room's centre exactly; a winner less than ~10 % closer than the runner-up is an
# effective tie.  The value sits on a plateau (0.05 and 0.10 flag the same labels on the
# fixed Tyumen sheets) and is a convention, not a value fitted to one sheet; the
# sensitivity table is in `evaluation/phase12/FINAL_A_ROOM_IDENTITY_REPORT.md`.
AMBIGUITY_MARGIN = 0.10
# Margin at which the margin component of the link score saturates.
_MARGIN_SATURATION = 0.5
# Room numbers printed below this fraction of the page height are treated as
# title-block / stamp content and excluded (reported, not silently dropped).
_TITLE_BLOCK_Y_FRACTION = 0.82
# Legacy ``confidence`` when nothing was measured (bare text-layer room label).
UNMEASURED_CONFIDENCE = 0.5
WALL_CHECK_STATUS = "not_evaluated"


def _bbox(word: dict[str, Any]) -> list[float]:
    return [float(value) for value in word.get("bbox", ())[:4]]


def _center(box: Iterable[float]) -> tuple[float, float]:
    left, top, right, bottom = map(float, box)
    return (left + right) / 2.0, (top + bottom) / 2.0


def _union(boxes: Iterable[Iterable[float]]) -> list[float]:
    rows = [list(map(float, box)) for box in boxes if box and len(box) >= 4]
    if not rows:
        return []
    return [min(row[0] for row in rows), min(row[1] for row in rows),
            max(row[2] for row in rows), max(row[3] for row in rows)]


def _pad(box: list[float], width: float, height: float) -> list[float]:
    if len(box) != 4:
        return box
    margin = max(6.0, min(width, height) * 0.012)
    return [max(0.0, box[0] - margin), max(0.0, box[1] - margin),
            min(width, box[2] + margin), min(height, box[3] + margin)]


def _word_confidence(word: dict[str, Any] | None) -> float | None:
    """Recognition confidence of a word: only a real OCR value, never invented."""
    value = (word or {}).get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value / 100.0 if value > 1.0 else value


def _min_known(*values: float | None) -> float | None:
    known = [value for value in values if value is not None]
    return min(known) if known else None


def normalize_system_label(value: str) -> tuple[str, str] | None:
    """Return `(kind, canonical label)` for P2, ВЕ, В2.4 and similar markers."""
    text = re.sub(r"\s+", "", str(value or "").strip(".,;:()[]{}" )).upper().replace(",", ".")
    match = re.fullmatch(r"([PРП])(\d{1,2})(?:\.(\d{1,2}))?", text)
    if match:
        label = f"P{int(match.group(2))}" + (f".{int(match.group(3))}" if match.group(3) else "")
        return "supply", label
    if re.fullmatch(r"[ВVB]Е", text):
        return "general_exhaust", "VE"
    match = re.fullmatch(r"[ВVB](\d{1,2})\.(\d{1,2})", text)
    if match:
        return "branch", f"V{int(match.group(1))}.{int(match.group(2))}"
    match = re.fullmatch(r"[ВVB](\d{1,2})", text)
    if match:
        return "exhaust_system", f"V{int(match.group(1))}"
    return None


# --------------------------------------------------------------------------- sheet context

# A floor is read only from title-like phrases ("План 3 этажа", "Экспликация помещений 3 этажа",
# "План подвала"): a bare "2 этаж" in a legend, a room name or a unit table says nothing about the
# floor this sheet depicts, and a schematic ("Схема ... установок 1 этаж") names the floor it serves,
# not the floor of its rooms.  Plural / adjective forms ("25 этажей", "9-этажный") never match.
_FLOOR_N = r"(-?\d{1,2})\s*[-–]?\s*(?:го|й|ый|ой|ом|ий|ое)?\s*этаж(?:а|е|у|ом)?(?![а-я])"
_FLOOR_NAMED = r"(подвал|цокол|кровл|чердак)\w*"
_FLOOR_PLAN_NUMBER = re.compile(r"план\w*[^.;:]{0,50}?(?<![\d.,])" + _FLOOR_N)
_FLOOR_EXPLICATION_NUMBER = re.compile(r"экспликац\w*\s+помещений\s+(?<![\d.,])" + _FLOOR_N)
_FLOOR_PLAN_NAMED = re.compile(r"(?:план|экспликац\w*\s+помещений)\w*[^.;:]{0,40}?" + _FLOOR_NAMED)
_FLOOR_LEVEL = re.compile(r"план\w*\s+(?:на\s+)?отм\.?\s*([+\-±]?\d{1,3}[.,]\d{3})")
_NAMED_FLOOR = {"подвал": "подвал", "цокол": "цоколь", "кровл": "кровля", "чердак": "чердак"}
_BUILDING = re.compile(
    r"(?:корпус\w*|секци[яиюе]|блок[-\s]?секци[яиюе]|литер\w*)\s*(?:№|n)?\s*"
    r"([0-9]{1,2}(?:[-.][0-9]{1,2})?[а-я]?|[а-я])(?![а-я0-9])"
)


def _sheet_text(snapshot: dict[str, Any]) -> str:
    text = str(snapshot.get("text") or "")
    if not text.strip():
        text = " ".join(str(word.get("text") or "") for word in snapshot.get("words") or [])
    return re.sub(r"\s+", " ", text.casefold().replace("ё", "е"))


def detect_sheet_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Read floor and building from the sheet text.

    Nothing is guessed: if several different floors (or buildings) are named on
    the sheet, or none is, the value is ``None`` with ``*_status`` saying why.
    """
    text = _sheet_text(snapshot)
    floors: set[str] = set()
    for pattern in (_FLOOR_PLAN_NUMBER, _FLOOR_EXPLICATION_NUMBER):
        for match in pattern.finditer(text):
            floors.add(str(int(match.group(1))))
    for match in _FLOOR_PLAN_NAMED.finditer(text):
        floors.add(_NAMED_FLOOR[match.group(1)])
    level_marks = {m.group(1).replace(",", ".").replace("±", "+") for m in _FLOOR_LEVEL.finditer(text)}
    floor_source = "sheet_text"
    if not floors and level_marks:
        floors = {f"отм:{mark}" for mark in level_marks}
        floor_source = "sheet_level_mark"
    buildings = {m.group(1).upper() for m in _BUILDING.finditer(text)}
    return {
        "floor": next(iter(floors)) if len(floors) == 1 else None,
        "floor_status": "found" if len(floors) == 1 else ("multiple" if floors else "not_found"),
        "floor_source": floor_source if floors else None,
        "floors_seen": sorted(floors),
        "building": next(iter(buildings)) if len(buildings) == 1 else None,
        "building_status": "found" if len(buildings) == 1 else ("multiple" if buildings else "not_found"),
        "building_source": "sheet_text" if buildings else None,
        "buildings_seen": sorted(buildings),
    }


# --------------------------------------------------------------------------- rooms

def _room_candidates(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Room labels on the drawing area, and those dropped as title-block content."""
    height = float(snapshot.get("height") or 1.0)
    words = snapshot.get("words") or []
    accepted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[float, ...]]] = set()
    for index, word in enumerate(words):
        box = _bbox(word)
        if len(box) != 4:
            continue
        token = str(word.get("text") or "").strip(".,;:()[]{}")
        if not _ROOM_TOKEN.fullmatch(token) or token in {"0", "00", "000", "0000"}:
            continue
        # One- and two-digit dimensions/sheet labels are common; accept them as
        # room numbers only when the adjacent text explicitly calls them rooms.
        digits = re.sub(r"\D", "", token)
        if len(digits) <= 2 and not re.search(r"[.,-]|[А-ЯA-Z]", token, re.IGNORECASE):
            nearby = " ".join(str(item.get("text") or "") for item in words[max(0, index - 3):index + 4])
            if not _ROOM_CONTEXT.search(nearby):
                continue
        key = (token, tuple(box))
        if key in seen:
            continue
        seen.add(key)
        row = {"room": token, "bbox": box, "word": word}
        (excluded if box[1] >= height * _TITLE_BLOCK_Y_FRACTION else accepted).append(row)
    return accepted, excluded


def extract_room_words(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Find room labels while preserving their exact printed form."""
    return _room_candidates(snapshot)[0]


def excluded_zone_rooms(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Room-like labels in the bottom title-block strip: excluded, but visible."""
    return [{"room": row["room"], "bbox": row["bbox"]} for row in _room_candidates(snapshot)[1]]


def extract_system_words(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for word in snapshot.get("words") or []:
        text = str(word.get("text") or "").strip()
        normalized = normalize_system_label(text)
        if normalized:
            output.append({"kind": normalized[0], "label": normalized[1], "bbox": _bbox(word), "word": word})
    return output


def _label_sort_key(value: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", value))


def _sorted_rooms(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rooms, key=lambda row: (row["bbox"][1], row["bbox"][0], row["room"]))
    return [{**row, "instance": index} for index, row in enumerate(ordered)]


def _link_score(distance: float, runner_up: float | None, radius: float, linkage: str) -> float:
    """Geometric ranking score in [0.05, 0.95]; NOT a probability.

    Half is proximity inside the link window, half is how clearly the winner
    beats the runner-up; a same-row fallback link is discounted.
    """
    proximity = max(0.0, 1.0 - distance / radius) if radius else 0.0
    margin = 1.0 if runner_up is None else ((runner_up - distance) / runner_up if runner_up > 0 else 0.0)
    score = 0.5 * proximity + 0.5 * min(1.0, margin / _MARGIN_SATURATION)
    if linkage == "same_row_fallback":
        score *= 0.8
    return round(min(0.95, max(0.05, score)), 3)


def room_system_links(
    snapshot: dict[str, Any],
    *,
    kinds: set[str] | None = None,
    same_row_fallback: bool = True,
) -> dict[str, Any]:
    """Link system labels to rooms; ambiguity and excluded rooms are returned, not hidden.

    Returns ``{"rows", "ambiguous", "excluded_zone_rooms", "sheet", "wall_check"}``.
    One row per *room label instance*: the same number printed twice on a sheet
    yields two rows (the comparison turns that into a duplicate-key conflict).
    """
    width, height = float(snapshot["width"]), float(snapshot["height"])
    context = detect_sheet_context(snapshot)
    accepted, excluded = _room_candidates(snapshot)
    rooms = _sorted_rooms(accepted)
    systems = [row for row in extract_system_words(snapshot) if kinds is None or row["kind"] in kinds]
    systems.sort(key=lambda row: (row["bbox"][1], row["bbox"][0], row["label"]))
    primary_radius = math.hypot(*_PRIMARY_WINDOW)
    fallback_radius = math.hypot(*_FALLBACK_WINDOW)

    assigned: dict[int, list[dict[str, Any]]] = {}
    ambiguous: list[dict[str, Any]] = []
    for system in systems:
        sx, sy = _center(system["bbox"])
        found = []
        for room in rooms:
            rx, ry = _center(room["bbox"])
            dx, dy = abs(sx - rx) / max(width, 1.0), abs(sy - ry) / max(height, 1.0)
            distance = (dx * dx + dy * dy) ** 0.5
            if dx <= _PRIMARY_WINDOW[0] and dy <= _PRIMARY_WINDOW[1]:
                found.append((distance, "primary_proximity", primary_radius, room))
            elif same_row_fallback and dx <= _FALLBACK_WINDOW[0] and dy <= _FALLBACK_WINDOW[1]:
                found.append((distance, "same_row_fallback", fallback_radius, room))
        if not found:
            continue
        found.sort(key=lambda item: (item[0], item[3]["instance"]))
        best, runner_up = found[0], (found[1] if len(found) > 1 else None)
        if runner_up is not None and (runner_up[0] - best[0]) <= AMBIGUITY_MARGIN * runner_up[0]:
            near = [item for item in found if (item[0] - best[0]) <= AMBIGUITY_MARGIN * item[0]]
            ambiguous.append({
                "label": system["label"], "kind": system["kind"], "bbox": system["bbox"],
                "text_confidence": _word_confidence(system["word"]),
                "reason": "AMBIGUOUS_SYSTEM_LINK",
                "candidates": [
                    {"location": item[3]["room"], "instance": item[3]["instance"],
                     "bbox": item[3]["bbox"], "distance": round(item[0], 4), "linkage": item[1]}
                    for item in near
                ],
            })
            continue
        distance, linkage, radius, room = best
        assigned.setdefault(room["instance"], []).append({
            "system": system, "distance": distance, "linkage": linkage,
            "link_confidence": _link_score(distance, runner_up[0] if runner_up else None, radius, linkage),
        })

    ambiguous_by_instance: dict[int, set[str]] = {}
    for item in ambiguous:
        for candidate in item["candidates"]:
            ambiguous_by_instance.setdefault(candidate["instance"], set()).add(item["label"])

    rows: list[dict[str, Any]] = []
    for instance, links in assigned.items():
        room = rooms[instance]
        chosen = [link["system"] for link in links]
        labels = sorted({row["label"] for row in chosen}, key=_label_sort_key)
        if not labels:
            continue
        zone = _pad(_union([room["bbox"], *(row["bbox"] for row in chosen)]), width, height)
        text_confidence = _min_known(_word_confidence(room["word"]), *(_word_confidence(row["word"]) for row in chosen))
        link_confidence = min(link["link_confidence"] for link in links)
        linkage = "primary_proximity" if any(l["linkage"] == "primary_proximity" for l in links) else "same_row_fallback"
        rows.append({
            "location": room["room"], "systems": labels,
            "normalized_value": ",".join(labels), "bbox_pdf": zone,
            "room_bbox": room["bbox"],
            "text_confidence": text_confidence, "link_confidence": link_confidence,
            # legacy field: minimum of the measured components (documented above)
            "confidence": _min_known(text_confidence, link_confidence),
            "linkage": linkage, "wall_check": WALL_CHECK_STATUS,
            "ambiguous_systems": sorted(ambiguous_by_instance.get(instance, set()) - set(labels), key=_label_sort_key),
            "floor": context["floor"], "building": context["building"],
            "floor_source": context["floor_source"], "floor_status": context["floor_status"],
            "sheet": snapshot.get("sheet") or "", "sheet_page": snapshot.get("page"),
            "instance": instance,
            "context": f"Помещение {room['room']}; связанные системы: {', '.join(labels)}",
            "snapshot": snapshot,
        })
    rows.sort(key=lambda row: (row["location"].casefold(), row["bbox_pdf"][1], row["bbox_pdf"][0]))
    return {
        "rows": rows, "ambiguous": ambiguous, "sheet": context,
        "excluded_zone_rooms": [{"room": row["room"], "bbox": row["bbox"]} for row in excluded],
        "wall_check": WALL_CHECK_STATUS,
    }


def room_system_rows(
    snapshot: dict[str, Any],
    *,
    kinds: set[str] | None = None,
    same_row_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Attach nearby system labels to each room and return a zone bbox per room.

    Ambiguous labels are *not* attached; use :func:`room_system_links` to see them.
    """
    return room_system_links(snapshot, kinds=kinds, same_row_fallback=same_row_fallback)["rows"]


# --------------------------------------------------------------------------- comparison

def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _signature(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(set(str(s) for s in row.get("systems") or ()), key=lambda v: (_label_sort_key(v), v)))


def _row_order(row: dict[str, Any]) -> tuple:
    box = tuple(float(v) for v in (row.get("bbox_pdf") or ())[:4])
    return (_clean(row.get("sheet")), _clean(row.get("sheet_page")), box, _signature(row), _clean(row.get("location")))


def _identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """(building, floor, sheet scope, number); scope is set only when the floor is unknown."""
    floor = _clean(row.get("floor"))
    scope = "" if floor else _clean(row.get("sheet")) + "|" + _clean(row.get("sheet_page"))
    return (_clean(row.get("building")), floor, scope, _clean(row.get("location")))


def _group_side(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group one stage's rows by full identity; keep every record of a conflicting key."""
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(_identity(row), []).append(row)
    groups = []
    for key in sorted(buckets):
        records = sorted(buckets[key], key=_row_order)
        signatures = {_signature(record) for record in records}
        # identical duplicates carry no contradiction: keep the best-measured one
        representative = max(records, key=lambda r: (float(r.get("confidence") or 0.0),) ) if len(signatures) == 1 else None
        if representative is not None:
            ties = [r for r in records if float(r.get("confidence") or 0.0) == float(representative.get("confidence") or 0.0)]
            representative = ties[0]
        groups.append({"key": key, "records": records, "conflict": len(signatures) > 1,
                       "row": representative, "duplicates": len(records)})
    return groups


_NAMED_FLOORS = set(_NAMED_FLOOR.values())


def floors_relation(left: str, right: str) -> str:
    """'equal' / 'different' / 'unknown' for two floor labels (empty = not determined)."""
    if not left or not right:
        return "unknown"
    if left == right:
        return "equal"
    left_named, right_named = left in _NAMED_FLOORS, right in _NAMED_FLOORS
    left_level, right_level = left.startswith("отм:"), right.startswith("отм:")
    if left_level or right_level:
        return "unknown"                                  # level marks and floor numbers use different notations
    if left_named and right_named:
        return "different"
    if left_named or right_named:
        number = right if left_named else left
        named = left if left_named else right
        # a basement may be written as floor -1 / 0; a positive floor is never a basement or a roof
        if number.lstrip("-").isdigit() and int(number) <= 0 and named in {"подвал", "цоколь"}:
            return "unknown"
        return "different"
    return "different"


def _compatible(left: tuple[str, str, str, str], right: tuple[str, str, str, str]) -> bool:
    if left[3] != right[3]:
        return False
    if left[0] and right[0] and left[0] != right[0]:
        return False
    if floors_relation(left[1], right[1]) == "different":
        return False
    return True


def _weak(left: tuple[str, str, str, str], right: tuple[str, str, str, str]) -> bool:
    """True when the floor or building could not be compared (one side unknown)."""
    floor_unknown = floors_relation(left[1], right[1]) != "equal"
    building_unknown_on_one_side = bool(left[0]) != bool(right[0])
    return floor_unknown or building_unknown_on_one_side


def _plain(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "snapshot"}


def compare_project_to_working(
    project_rows: Iterable[dict[str, Any]],
    working_rows: Iterable[dict[str, Any]],
    *,
    mode: str = "disappearance",
    include_unresolved: bool = False,
) -> list[dict[str, Any]]:
    """Compare project room signatures with working drawings.

    ``mode="disappearance"`` reports rooms that lost a system; ``mode="composition"``
    also reports rooms that gained one.  Missing and added systems are always
    kept apart; neither is a verdict, the subject rule decides what an addition
    means.  Rooms are matched by (building, floor, number) with sheet scope when
    the floor is unknown.  Duplicate full keys give ``CONFLICT`` /
    ``DUPLICATE_ROOM_KEY``; several compatible counterparts give ``AMBIGUOUS`` /
    ``AMBIGUOUS_ROOM_MATCH``; neither decides anything.  Labels whose room link
    was ambiguous on the sheet are neither missing nor added (``unresolved_systems``).
    The result does not depend on the order of the input rows.
    """
    if mode not in {"disappearance", "composition"}:
        raise ValueError(f"unknown mode {mode!r}")
    entries = []
    for p_groups, w_groups in _components(project_rows, working_rows):
        entry = _component_entry(p_groups, w_groups, mode, include_unresolved)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda e: (e["location"], e["building"], e["floor"], e["status"], _canonical_key(e)))
    return entries


def _components(project_rows, working_rows):
    """Connected components of the 'could be the same room' relation (PD groups, RD groups)."""
    project = _group_side(project_rows)
    working = _group_side(working_rows)
    edges: dict[tuple[str, int], set[tuple[str, int]]] = {}
    for p_index, p_group in enumerate(project):
        for w_index, w_group in enumerate(working):
            if _compatible(p_group["key"], w_group["key"]):
                edges.setdefault(("P", p_index), set()).add(("W", w_index))
                edges.setdefault(("W", w_index), set()).add(("P", p_index))
    seen: set[tuple[str, int]] = set()
    result = []
    for p_index in range(len(project)):
        if ("P", p_index) in seen:
            continue
        stack, component = [("P", p_index)], []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            component.append(node)
            stack.extend(sorted(edges.get(node, ())))
        result.append((
            [project[i] for i in sorted(i for side, i in component if side == "P")],
            [working[i] for i in sorted(i for side, i in component if side == "W")],
        ))
    return result


def match_room_rows(project_rows: Iterable[dict[str, Any]], working_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair project and working rooms without judging their systems.

    Each result has ``kind`` in ``MATCHED`` / ``UNMATCHED`` (no working counterpart) /
    ``CONFLICT`` (``DUPLICATE_ROOM_KEY``) / ``AMBIGUOUS`` (``AMBIGUOUS_ROOM_MATCH``).
    """
    output = []
    for p_groups, w_groups in _components(project_rows, working_rows):
        entry = _component_entry(p_groups, w_groups, "composition", True, pair_only=True)
        output.append(entry)
    output.sort(key=lambda e: (e["location"], e["building"], e["floor"], e["kind"], _canonical_key(e)))
    return output


def keys_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Could two rows (stage-independent) denote the same room?"""
    return _compatible(_identity(left), _identity(right))


def group_by_identity(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group one stage's rows by room identity (see ``compare_project_to_working``)."""
    return _group_side(rows)


def _canonical_key(entry: dict[str, Any]) -> str:
    return repr(sorted((side, [_row_order(r) for r in rows]) for side, rows in (entry.get("records") or {}).items()))


def _component_entry(p_groups, w_groups, mode, include_unresolved, pair_only=False):
    location = p_groups[0]["key"][3]
    building = next((g["key"][0] for g in [*p_groups, *w_groups] if g["key"][0]), "")
    floor = next((g["key"][1] for g in [*p_groups, *w_groups] if g["key"][1]), "")
    records = {"PD": [_plain(r) for g in p_groups for r in g["records"]],
               "RD": [_plain(r) for g in w_groups for r in g["records"]]}
    base = {"location": location, "building": building, "floor": floor, "mode": mode,
            "missing_systems": [], "added_systems": [], "unresolved_systems": [],
            "expected": None, "actual": None, "records": records, "match_basis": None,
            "candidates": [], "reason": None}
    if any(g["conflict"] for g in [*p_groups, *w_groups]):
        sides = [name for name, groups in (("PD", p_groups), ("RD", w_groups)) if any(g["conflict"] for g in groups)]
        return {**base, "status": "CONFLICT", "kind": "CONFLICT", "reason": "DUPLICATE_ROOM_KEY", "conflict_sides": sides}
    if len(p_groups) > 1 or len(w_groups) > 1:
        return {**base, "status": "AMBIGUOUS", "kind": "AMBIGUOUS", "reason": "AMBIGUOUS_ROOM_MATCH",
                "candidates": [{"stage": stage, "key": list(g["key"])}
                               for stage, groups in (("PD", p_groups), ("RD", w_groups)) for g in groups]}
    expected = p_groups[0]["row"]
    expected_set = set(_signature(expected))
    expected_unsure = set(expected.get("ambiguous_systems") or ())
    if not w_groups:
        if pair_only:
            return {**base, "status": "UNMATCHED", "kind": "UNMATCHED", "expected": expected}
        missing = sorted(expected_set, key=lambda v: (_label_sort_key(v), v))
        if not missing:
            return None
        return {**base, "status": "MISSING", "kind": "UNMATCHED", "expected": expected, "missing_systems": missing,
                "room_not_found_in_working": True}
    actual = w_groups[0]["row"]
    actual_set = set(_signature(actual))
    actual_unsure = set(actual.get("ambiguous_systems") or ())
    unresolved = (expected_set - actual_set) & actual_unsure | (actual_set - expected_set) & expected_unsure
    missing = sorted((expected_set - actual_set) - unresolved, key=lambda v: (_label_sort_key(v), v))
    added = sorted((actual_set - expected_set) - unresolved, key=lambda v: (_label_sort_key(v), v))
    unresolved_list = sorted(unresolved, key=lambda v: (_label_sort_key(v), v))
    basis = "room_number_only" if _weak(p_groups[0]["key"], w_groups[0]["key"]) else "room_key"
    base.update(expected=expected, actual=actual, match_basis=basis,
                missing_systems=missing, added_systems=added, unresolved_systems=unresolved_list,
                kind="MATCHED")
    if pair_only:
        return {**base, "status": "MATCHED"}
    if missing and added:
        status = "MISSING_AND_ADDED" if mode == "composition" else "MISSING"
    elif missing:
        status = "MISSING"
    elif added and mode == "composition":
        status = "ADDED"
    elif unresolved_list and include_unresolved:
        return {**base, "status": "UNRESOLVED_LINK", "reason": "AMBIGUOUS_SYSTEM_LINK"}
    else:
        return None
    return {**base, "status": status}


# --------------------------------------------------------------------------- zones

def room_zone_index(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """One evidence zone per room-label instance, in a deterministic order."""
    width, height = float(snapshot["width"]), float(snapshot["height"])
    context = detect_sheet_context(snapshot)
    linked = {row["instance"]: row for row in room_system_links(snapshot, same_row_fallback=False)["rows"]}
    zones = []
    for room in _sorted_rooms(extract_room_words(snapshot)):
        row = linked.get(room["instance"])
        if row is not None:
            zones.append(row)
            continue
        text_confidence = _word_confidence(room["word"])
        zones.append({
            "location": room["room"], "systems": [], "normalized_value": "",
            "bbox_pdf": _pad(room["bbox"], width, height), "room_bbox": room["bbox"],
            "text_confidence": text_confidence, "link_confidence": None,
            "confidence": text_confidence if text_confidence is not None else UNMEASURED_CONFIDENCE,
            "floor": context["floor"], "building": context["building"],
            "floor_source": context["floor_source"], "floor_status": context["floor_status"],
            "sheet": snapshot.get("sheet") or "", "sheet_page": snapshot.get("page"),
            "instance": room["instance"],
            "context": f"Помещение {room['room']}; зона помещения на листе", "snapshot": snapshot,
        })
    return zones


def room_zone_for_location(snapshot: dict[str, Any], location: str) -> dict[str, Any] | None:
    """Return the printed room zone, including nearby system labels when found."""
    return room_zones_for_snapshot(snapshot).get(str(location))


def room_zones_for_snapshot(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Room-local evidence zones keyed by printed number, built once per page.

    When one number is printed several times on the sheet the first zone (by
    position) is returned and flagged ``duplicate_on_sheet`` with the count;
    the caller must not treat such a zone as a unique room.
    """
    by_number: dict[str, list[dict[str, Any]]] = {}
    for zone in room_zone_index(snapshot):
        by_number.setdefault(zone["location"], []).append(zone)
    zones = {}
    for number, items in by_number.items():
        first = dict(items[0])
        if len(items) > 1:
            first["duplicate_on_sheet"] = True
            first["instances_on_sheet"] = len(items)
        zones[number] = first
    return zones


def warm_floor_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Find explicitly labelled warm-floor contours by room on a heating plan."""
    width, height = float(snapshot["width"]), float(snapshot["height"])
    words = snapshot.get("words") or []
    context = detect_sheet_context(snapshot)
    page_text = str(snapshot.get("text") or "")
    has_warm_floor_context = bool(_FLOOR_MARKER.search(page_text))
    markers = []
    for word in words:
        text = str(word.get("text") or "")
        if _FLOOR_MARKER.search(text) or (has_warm_floor_context and _FLOOR_ABBREVIATION.search(text)):
            markers.append({"bbox": _bbox(word), "text": text, "word": word})
    # Join split text words such as "Теплый" + "пол" without broadening the
    # match to every room on a sheet that mentions warm floors in its legend.
    for index, word in enumerate(words):
        left = str(word.get("text") or "").casefold().replace("ё", "е")
        right = str(words[index + 1].get("text") or "").casefold().replace("ё", "е") if index + 1 < len(words) else ""
        if left.startswith("тепл") and right.startswith("пол"):
            markers.append({"bbox": _union((_bbox(word), _bbox(words[index + 1]))), "text": f"{left} {right}",
                            "word": word})
    window_radius = math.hypot(0.12, 0.16)
    output = []
    for room in _sorted_rooms(extract_room_words(snapshot)):
        rx, ry = _center(room["bbox"])
        nearby = []
        for marker in markers:
            mx, my = _center(marker["bbox"])
            dx, dy = abs(mx - rx) / max(width, 1.0), abs(my - ry) / max(height, 1.0)
            if dx <= 0.12 and dy <= 0.16:
                nearby.append((marker, (dx * dx + dy * dy) ** 0.5))
        if not nearby:
            continue
        nearby.sort(key=lambda row: row[1])
        chosen = [row[0] for row in nearby if row[1] <= nearby[0][1] + 0.04]
        zone = _pad(_union([room["bbox"], *(marker["bbox"] for marker in chosen)]), width, height)
        runner_up = next((row[1] for row in nearby if row[0] not in chosen), None)
        link_confidence = _link_score(nearby[0][1], runner_up, window_radius, "primary_proximity")
        text_confidence = _min_known(_word_confidence(room["word"]), *(_word_confidence(m.get("word")) for m in chosen))
        output.append({"location": room["room"], "systems": ["WARM_FLOOR"],
                       "normalized_value": "WARM_FLOOR", "bbox_pdf": zone,
                       "room_bbox": room["bbox"],
                       "text_confidence": text_confidence, "link_confidence": link_confidence,
                       "confidence": _min_known(text_confidence, link_confidence),
                       "floor": context["floor"], "building": context["building"],
                       "floor_source": context["floor_source"], "floor_status": context["floor_status"],
                       "sheet": snapshot.get("sheet") or "", "sheet_page": snapshot.get("page"),
                       "instance": room["instance"],
                       "context": f"Помещение {room['room']}; контур теплого пола",
                       "snapshot": snapshot})
    return output
