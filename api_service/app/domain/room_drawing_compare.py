"""Room-local extraction and comparison for HVAC plan and schematic sheets.

Locations remain exactly as printed on the sheet (including leading zeroes).
System labels are normalized only enough to compare common Cyrillic/Latin
letter variants and punctuation used in ventilation drawings.
"""
from __future__ import annotations

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


def extract_room_words(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Find room labels while preserving their exact printed form."""
    height = float(snapshot.get("height") or 1.0)
    words = snapshot.get("words") or []
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[float, ...]]] = set()
    for index, word in enumerate(words):
        box = _bbox(word)
        if len(box) != 4 or box[1] >= height * 0.82:
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
        output.append({"room": token, "bbox": box, "word": word})
    return output


def extract_system_words(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for word in snapshot.get("words") or []:
        text = str(word.get("text") or "").strip()
        normalized = normalize_system_label(text)
        if normalized:
            output.append({"kind": normalized[0], "label": normalized[1], "bbox": _bbox(word), "word": word})
    return output


def room_system_rows(
    snapshot: dict[str, Any],
    *,
    kinds: set[str] | None = None,
    same_row_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Attach nearby system labels to each room and return a zone bbox per room."""
    width, height = float(snapshot["width"]), float(snapshot["height"])
    rooms = extract_room_words(snapshot)
    systems = [row for row in extract_system_words(snapshot) if kinds is None or row["kind"] in kinds]
    assigned: dict[str, list[dict[str, Any]]] = {}
    linkage_by_room: dict[str, str] = {}
    room_by_location = {room["room"]: room for room in rooms}
    for system in systems:
        sx, sy = _center(system["bbox"])
        matches = []
        for room in rooms:
            rx, ry = _center(room["bbox"])
            dx, dy = abs(sx - rx) / max(width, 1.0), abs(sy - ry) / max(height, 1.0)
            if dx <= 0.095 and dy <= 0.17:
                matches.append(((dx * dx + dy * dy) ** 0.5, "primary_proximity", room["room"]))
            elif same_row_fallback and dx <= 0.14 and dy <= 0.055:
                matches.append(((dx * dx + dy * dy) ** 0.5, "same_row_fallback", room["room"]))
        if not matches:
            continue
        _distance, linkage, location = min(matches, key=lambda row: (row[0], row[2]))
        assigned.setdefault(location, []).append(system)
        previous = linkage_by_room.get(location)
        if previous != "primary_proximity":
            linkage_by_room[location] = linkage

    output: list[dict[str, Any]] = []
    for location, chosen in assigned.items():
        room = room_by_location[location]
        labels = sorted({row["label"] for row in chosen}, key=lambda value: tuple(int(x) for x in re.findall(r"\d+", value)))
        if not labels:
            continue
        zone = _pad(_union([room["bbox"], *(row["bbox"] for row in chosen)]), width, height)
        output.append({
            "location": location, "systems": labels,
            "normalized_value": ",".join(labels), "bbox_pdf": zone,
            "room_bbox": room["bbox"], "confidence": 0.88,
            "linkage": linkage_by_room.get(location, "primary_proximity"),
            "context": f"Помещение {location}; связанные системы: {', '.join(labels)}",
            "snapshot": snapshot,
        })
    return sorted(output, key=lambda row: (row["location"].casefold(), row["bbox_pdf"][1], row["bbox_pdf"][0]))


def compare_project_to_working(
    project_rows: Iterable[dict[str, Any]], working_rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return project room signatures absent or changed in working drawings."""
    pd_by_room = {str(row["location"]): row for row in project_rows}
    rd_by_room = {str(row["location"]): row for row in working_rows}
    changes = []
    for room, expected in sorted(pd_by_room.items()):
        actual = rd_by_room.get(room)
        expected_set = set(expected.get("systems") or ())
        actual_set = set(actual.get("systems") or ()) if actual else set()
        missing = sorted(expected_set - actual_set)
        if missing:
            changes.append({"location": room, "expected": expected, "actual": actual,
                            "missing_systems": missing})
    return changes


def room_zone_for_location(snapshot: dict[str, Any], location: str) -> dict[str, Any] | None:
    """Return the printed room zone, including nearby system labels when found."""
    return room_zones_for_snapshot(snapshot).get(str(location))


def room_zones_for_snapshot(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build room-local evidence zones once for a page instead of per room."""
    width, height = float(snapshot["width"]), float(snapshot["height"])
    zones = {
        row["location"]: row
        for row in room_system_rows(snapshot, same_row_fallback=False)
    }
    for room in extract_room_words(snapshot):
        if room["room"] not in zones:
            zone = _pad(room["bbox"], width, height)
            zones[room["room"]] = {
                "location": room["room"], "systems": [], "normalized_value": "",
                "bbox_pdf": zone, "room_bbox": room["bbox"], "confidence": 0.72,
                "context": f"Помещение {room['room']}; зона помещения на листе", "snapshot": snapshot,
            }
    return zones


def warm_floor_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Find explicitly labelled warm-floor contours by room on a heating plan."""
    width, height = float(snapshot["width"]), float(snapshot["height"])
    words = snapshot.get("words") or []
    page_text = str(snapshot.get("text") or "")
    has_warm_floor_context = bool(_FLOOR_MARKER.search(page_text))
    markers = []
    for word in words:
        text = str(word.get("text") or "")
        if _FLOOR_MARKER.search(text) or (has_warm_floor_context and _FLOOR_ABBREVIATION.search(text)):
            markers.append({"bbox": _bbox(word), "text": text})
    # Join split text words such as "Теплый" + "пол" without broadening the
    # match to every room on a sheet that mentions warm floors in its legend.
    for index, word in enumerate(words):
        left = str(word.get("text") or "").casefold().replace("ё", "е")
        right = str(words[index + 1].get("text") or "").casefold().replace("ё", "е") if index + 1 < len(words) else ""
        if left.startswith("тепл") and right.startswith("пол"):
            markers.append({"bbox": _union((_bbox(word), _bbox(words[index + 1]))), "text": f"{left} {right}"})
    output = []
    for room in extract_room_words(snapshot):
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
        output.append({"location": room["room"], "systems": ["WARM_FLOOR"],
                       "normalized_value": "WARM_FLOOR", "bbox_pdf": zone,
                       "room_bbox": room["bbox"], "confidence": 0.8,
                       "context": f"Помещение {room['room']}; контур теплого пола",
                       "snapshot": snapshot})
    return output
