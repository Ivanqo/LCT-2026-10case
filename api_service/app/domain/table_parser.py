"""Room / apartment schedule tables read straight from a page's text layer (Phase 12, S1).

Finds the tables a drawing set uses to state its rooms and apartments -- «Экспликация помещений»,
«Спецификация помещений», «Спецификация квартир», «Ведомость помещений» -- on a page snapshot of the
`dataset_sources.extract_original_pages` contract (flat word list, visible-frame bboxes, page width/height),
and returns them as structured rows: key (room / apartment number exactly as printed), name, numeric cells
(area, count, ...) and the floor totals («Итог(о) по этажу», «Итого:»). Pure: no DB, no fitz, no clock.

How a table is recognised (geometry + vocabulary, never file names / page numbers / object constants):
  header   a «Номер»/«№»/«Поз.» word whose header band (the text lines within ~2.6 word heights of it)
           continues to the right, with small gaps, into a name-like column («Наименование», «Имя»,
           «Назначение», «Тип») and a numeric column («Площадь», «Кол-во», «Общая», ...). Header words of
           several lines that overlap in X form one column; a second «Номер» after a name/area column starts
           the next side-by-side block of the same schedule (long schedules are printed in 2-6 blocks).
  rows     every word in the block's X range below the header whose text looks like a room key («12»,
           «1.109», «2.4.1», «А», «Ст.1.1.1», «2а») and starts left of the name column; a row owns the
           words between the midpoints to its neighbours, so a name wrapped onto 2-3 lines stays in its row.
           The block ends at a total row («Итог», «Итого», «Всего»), a vertical gap of several row pitches
           or a drawing title-block word.
  cells    a word belongs to the column whose span (midpoints between neighbouring header columns) holds
           its X centre; a non-key word inside the number column belongs to the next (name) column.
  title    the nearest «экспликация/спецификация/ведомость ...» line above the header over the block's X
           range; floor / section / building / scope qualifiers (МОП, БКТ, квартир) are parsed from it,
           falling back to the page's single «План ... этажа» caption when the title names no floor.

Overprinted text (a bold effect some CAD exporters produce by drawing each glyph run twice) is de-duplicated
first: a word with the same text and a nearly identical box as an earlier word is dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from statistics import median
from typing import Any, Iterable

from .anchor_search import cluster_rows, union_bbox

TABLE_PARSER_VERSION = "table-parser-v1"

KIND_ROOMS = "ROOMS"
KIND_APARTMENTS = "APARTMENTS"

ROLE_NUMBER = "NUMBER"
ROLE_NAME = "NAME"
ROLE_TYPE = "TYPE"
ROLE_AREA = "AREA"
ROLE_LIVING_AREA = "LIVING_AREA"
ROLE_TOTAL_AREA = "TOTAL_AREA"
ROLE_SUMMER_AREA = "SUMMER_AREA"
ROLE_COUNT = "COUNT"
ROLE_ROOMS = "ROOMS"
ROLE_CATEGORY = "CATEGORY"
ROLE_OTHER = "OTHER"
TEXT_ROLES = frozenset({ROLE_NAME, ROLE_TYPE})
NUMERIC_ROLES = frozenset({ROLE_AREA, ROLE_LIVING_AREA, ROLE_TOTAL_AREA, ROLE_SUMMER_AREA, ROLE_COUNT, ROLE_ROOMS})
AREA_ROLES = frozenset({ROLE_AREA, ROLE_LIVING_AREA, ROLE_TOTAL_AREA, ROLE_SUMMER_AREA})

_NUMBER_HEADER_WORDS = frozenset({"номер", "№", "№№", "поз", "позиция", "n", "№п/п", "№пом", "номер/тип"})
_NAME_HEADER_RE = re.compile(r"^(наимен|имя$|назван|назнач|помещени)")
_TYPE_HEADER_RE = re.compile(r"^тип")
_AREA_HEADER_RE = re.compile(r"^площад")
_COUNT_HEADER_RE = re.compile(r"^(кол-?во|количеств|шт$)")
_ROOMS_HEADER_RE = re.compile(r"^(комнат|комн)")
_CATEGORY_HEADER_RE = re.compile(r"^кат")
_LIVING_RE = re.compile(r"жил")
_TOTAL_RE = re.compile(r"общ")
_SUMMER_RE = re.compile(r"(летн|коэф|лодж|балк)")
_TITLE_RE = re.compile(r"(экспликац|спецификац|ведомост|сводн)", re.IGNORECASE)
_TITLE_SUBJECT_RE = re.compile(r"(помещ|квартир|площад)", re.IGNORECASE)
_NOT_ROOM_SCHEDULE_RE = re.compile(r"(отделк|экспликац\w*\s+пол|ведомост\w*\s+пол|потолк|дверн|дверей|оконн|окон(?:\s|$)|перемыч|перегород|витраж|ограждени|отмостк|благоустр)", re.IGNORECASE)
_GROUP_KEY_RE = re.compile(r"^(?:кв\.?\s?)?\d{1,3}\.\d{1,3}(?:\.\d{1,3})?$", re.IGNORECASE)
_TABLE_END_RE = re.compile(r"^(условн\w*\s+обозначен|примечани|примечание|общие указания)")
_TOTAL_WORD_RE = re.compile(r"^(итог|итого|всего)$", re.IGNORECASE)
# Title-block vocabulary of a Russian drawing stamp: a row starting with one of these ends a table region.
_STAMP_WORDS = frozenset({"изм", "изм.", "кол.уч", "кол.уч.", "лист", "листов", "стадия", "разработал", "проверил",
                          "н.контр", "н.контр.", "гип", "гап", "подп", "подп.", "дата", "формат", "инв", "взам"})
# A room / apartment key as printed: 12 | 012 | 1.109 | 2.4.1 | 1.39а | 2а | А | Б* | Ст.1.1.1 | Кв.12 | 1-12
ROOM_KEY_RE = re.compile(
    r"^(?:(?:ст|кв|пом|оф|пп|м/м)\.?\s?)?(?:\d{1,4}(?:[.\-/]\d{1,4}){0,3}[а-яa-z]?|[а-яa-z]\d{0,3}|\d{1,3}[а-яa-z])\*?$",
    re.IGNORECASE,
)
_DECIMAL_RE = re.compile(r"^-?\d{1,6}(?:[.,]\d{1,3})?$")
_UNIT_WORDS = frozenset({"м²", "м2", "м.кв", "кв.м", "кв.м.", "м", "шт", "шт."})
_HEADER_BAND_HEIGHTS = 2.6          # header lines within this many word heights of the «Номер» word
_HEADER_MAX_GAP_HEIGHTS = 16.0      # a larger horizontal gap between header words ends the header
_TITLE_SEARCH_HEIGHTS = 9.0         # how far above the header a table title may sit
_MAX_ROW_GAP_PITCHES = 3.2          # a vertical gap larger than this many row pitches ends the table
_MAX_ROWS = 400
_OVERPRINT_IOU = 0.6

_FLOOR_NUMBER_RE = re.compile(r"(?<![\d.,])(\d{1,2}(?:\s*[-–]\s*\d{1,2})?)\s*-?\s*(?:го|ого|й|ой|м|ый|ий)?\s*(?:этаж|эт\.)", re.IGNORECASE)
_FLOOR_WORDS = (
    (re.compile(r"(антресол|антесол|антресл)", re.IGNORECASE), "антресоль"),
    (re.compile(r"(подвал|подземн|минус\s*перв)", re.IGNORECASE), "подземный"),
    (re.compile(r"(техническ\w*\s+(этаж|подполь|пространств|чердак)|техэтаж|техподполь)", re.IGNORECASE), "технический"),
    (re.compile(r"мансард", re.IGNORECASE), "мансарда"),
    (re.compile(r"кровл", re.IGNORECASE), "кровля"),
    (re.compile(r"типов", re.IGNORECASE), "типовой"),
)
_SECTION_RE = re.compile(r"(?:секци[яиюей]\s*№?\s*([сc]?\d{1,2})|(\d{1,2})\s*-?\s*(?:я|й)?\s*секци|\b([сc]\d{1,2})\b)", re.IGNORECASE)
_BUILDING_RE = re.compile(r"(?:корпус\w*\s*№?\s*(\d{1,3}[а-я]?)|\bк\.\s*(\d{1,3}))", re.IGNORECASE)
_QUALIFIERS = (
    ("МОП", re.compile(r"(\bмоп\b|общего\s+пользован)", re.IGNORECASE)),
    ("БКТ", re.compile(r"\bбкт\b", re.IGNORECASE)),
    ("КВАРТИРЫ", re.compile(r"квартир", re.IGNORECASE)),
    ("НЕЖИЛЫЕ", re.compile(r"нежил|встроен|коммерч", re.IGNORECASE)),
    ("АВТОСТОЯНКА", re.compile(r"автостоян|паркинг", re.IGNORECASE)),
)


def norm(text: object) -> str:
    return str(text or "").lower().replace("ё", "е").strip()


def _norm_header(text: object) -> str:
    return norm(text).strip(" .,;:()")


def _box(word: dict[str, Any]) -> list[float]:
    return [float(v) for v in word["bbox"]]


def _cx(word: dict[str, Any]) -> float:
    b = word["bbox"]
    return (float(b[0]) + float(b[2])) / 2


def _cy(word: dict[str, Any]) -> float:
    b = word["bbox"]
    return (float(b[1]) + float(b[3])) / 2


def _h(word: dict[str, Any]) -> float:
    b = word["bbox"]
    return max(1.0, float(b[3]) - float(b[1]))


def _iou(a: list[float], b: list[float]) -> float:
    inter = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def dedupe_overprint(words: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drops a word drawn a second time over itself (same text, nearly the same box)."""
    kept: list[dict[str, Any]] = []
    by_text: dict[str, list[list[float]]] = {}
    for word in words:
        text = str(word.get("text") or "").strip()
        if not text or not word.get("bbox") or len(word["bbox"]) != 4:
            continue
        box = _box(word)
        seen = by_text.setdefault(text, [])
        if any(_iou(box, other) >= _OVERPRINT_IOU for other in seen):
            continue
        seen.append(box)
        kept.append(word)
    return kept


def parse_decimal(text: object) -> Decimal | None:
    raw = str(text or "").strip().replace(" ", "").replace(" ", "").rstrip(".")
    if not _DECIMAL_RE.match(raw):
        return None
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None


def decimals_of(text: object) -> int:
    raw = str(text or "").strip()
    match = re.search(r"[.,](\d+)$", raw)
    return len(match.group(1)) if match else 0


def normalize_key(key: object) -> str:
    """Comparison form of a room / apartment key: «Ст.1.1.1» ~ «1.1.1», «012» ~ «12», «1,109» ~ «1.109»."""
    text = norm(key).replace(",", ".").replace(" ", "").rstrip("*")
    text = re.sub(r"^(ст|кв|пом|оф|пп|м/м)\.?", "", text)
    parts = re.split(r"([.\-/])", text)
    return "".join(part.lstrip("0") or "0" if part.isdigit() else part for part in parts)


# ---------------------------------------------------------------- data shapes


@dataclass(slots=True)
class Column:
    role: str
    label: str
    x0: float
    x1: float
    span: tuple[float, float] = (0.0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "label": self.label, "x0": round(self.x0, 2), "x1": round(self.x1, 2),
                "span": [round(self.span[0], 2), round(self.span[1], 2)]}


@dataclass(slots=True)
class Row:
    key: str
    cells: dict[int, str]                       # column index -> cell text (reading order)
    bbox: list[float]
    key_bbox: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "cells": {str(k): v for k, v in sorted(self.cells.items())},
                "bbox": [round(v, 2) for v in self.bbox], "key_bbox": [round(v, 2) for v in self.key_bbox]}


@dataclass(slots=True)
class Total:
    label: str
    values: list[str]
    bbox: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "values": list(self.values), "bbox": [round(v, 2) for v in self.bbox]}


@dataclass(slots=True)
class Scope:
    floor: str | None = None
    section: str | None = None
    building: str | None = None
    qualifiers: tuple[str, ...] = ()
    source: str = "title"                       # title | page_caption | none

    def label(self) -> str:
        parts = []
        if self.building:
            parts.append(f"корпус {self.building}")
        if self.section:
            parts.append(f"секция {self.section}")
        if self.floor:
            parts.append(f"этаж {self.floor}" if self.floor.lstrip("-").isdigit() else self.floor)
        parts.extend(q.lower() for q in self.qualifiers)
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {"floor": self.floor, "section": self.section, "building": self.building,
                "qualifiers": list(self.qualifiers), "source": self.source}


@dataclass(slots=True)
class RoomTable:
    kind: str
    title: str
    title_bbox: list[float] | None
    scope: Scope
    columns: list[Column]
    rows: list[Row]
    totals: list[Total]
    bbox: list[float]
    page: int | None = None
    width: float = 0.0
    height: float = 0.0
    blocks: int = 1
    notes: list[str] = field(default_factory=list)

    def column_index(self, role: str) -> int | None:
        return next((i for i, col in enumerate(self.columns) if col.role == role), None)

    def bbox_norm(self, bbox: list[float] | None = None) -> list[float]:
        box = bbox or self.bbox
        w = self.width or 1.0
        h = self.height or 1.0
        return [round(min(1.0, max(0.0, v)), 6) for v in (box[0] / w, box[1] / h, box[2] / w, box[3] / h)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "title": self.title, "title_bbox": self.title_bbox, "scope": self.scope.to_dict(),
            "columns": [c.to_dict() for c in self.columns], "rows": [r.to_dict() for r in self.rows],
            "totals": [t.to_dict() for t in self.totals], "bbox": [round(v, 2) for v in self.bbox],
            "page": self.page, "width": self.width, "height": self.height, "blocks": self.blocks, "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoomTable":
        scope = data.get("scope") or {}
        return cls(
            kind=data["kind"], title=data.get("title") or "", title_bbox=data.get("title_bbox"),
            scope=Scope(floor=scope.get("floor"), section=scope.get("section"), building=scope.get("building"),
                        qualifiers=tuple(scope.get("qualifiers") or ()), source=scope.get("source") or "title"),
            columns=[Column(role=c["role"], label=c["label"], x0=c["x0"], x1=c["x1"], span=tuple(c.get("span") or (0, 0)))
                     for c in data.get("columns") or []],
            rows=[Row(key=r["key"], cells={int(k): v for k, v in (r.get("cells") or {}).items()}, bbox=list(r["bbox"]),
                      key_bbox=list(r["key_bbox"])) for r in data.get("rows") or []],
            totals=[Total(label=t["label"], values=list(t["values"]), bbox=list(t["bbox"])) for t in data.get("totals") or []],
            bbox=list(data["bbox"]), page=data.get("page"), width=float(data.get("width") or 0), height=float(data.get("height") or 0),
            blocks=int(data.get("blocks") or 1), notes=list(data.get("notes") or []),
        )


# ---------------------------------------------------------------- header detection


def _column_role(label: str) -> str:
    tokens = [t for t in re.split(r"[\s,]+", norm(label)) if t]
    joined = " ".join(tokens)
    first = tokens[0].strip(" .,;:()") if tokens else ""
    if first in _NUMBER_HEADER_WORDS or joined.startswith("номер"):
        return ROLE_NUMBER
    if _AREA_HEADER_RE.match(first) or ("площад" in joined and not _COUNT_HEADER_RE.match(first)):
        if _LIVING_RE.search(joined):
            return ROLE_LIVING_AREA
        if _SUMMER_RE.search(joined):
            return ROLE_SUMMER_AREA
        if _TOTAL_RE.search(joined):
            return ROLE_TOTAL_AREA
        return ROLE_AREA
    if _TOTAL_RE.match(first) and ("площ" in joined or "м²" in joined or "м2" in joined):
        return ROLE_TOTAL_AREA
    if _LIVING_RE.match(first):
        return ROLE_LIVING_AREA
    if _ROOMS_HEADER_RE.match(first) or ("комнат" in joined and _COUNT_HEADER_RE.match(first)):
        return ROLE_ROOMS
    if _COUNT_HEADER_RE.match(first):
        return ROLE_COUNT
    if _CATEGORY_HEADER_RE.match(first):
        return ROLE_CATEGORY
    if _TYPE_HEADER_RE.match(first):
        return ROLE_TYPE
    if _NAME_HEADER_RE.match(first):
        return ROLE_NAME
    return ROLE_OTHER


def _is_number_header(word: dict[str, Any]) -> bool:
    return _norm_header(word.get("text")) in _NUMBER_HEADER_WORDS


_HEADER_VOCAB_RE = re.compile(
    r"^(?:(?:номер|наимен|назван|назнач|площад|категор|колич|комнат|жил|общ|квартир|этаж|учет|летн|коэф|примеч|"
    r"позиц|марк|класс|функц|отделк|приведен|расчетн|полезн|помещ)\w*|"
    r"(?:№|n|п/п|пом|поме|ще|ния|имя|м2|м²|кв\.?м|кат|тип|кол-?во|кол|комн|кв|на|с|шт|ед|изм|поз|-|во)$)"
)


def _is_header_line(line: list[dict[str, Any]]) -> bool:
    tokens = [_norm_header(w.get("text")) for w in line]
    tokens = [t for t in tokens if t]
    if not tokens or any(parse_decimal(t) is not None for t in tokens):
        return False
    if _TITLE_RE.search(" ".join(tokens)):
        return False
    hits = sum(1 for t in tokens if _HEADER_VOCAB_RE.match(t))
    return hits * 2 >= len(tokens)


def _header_band(anchor: dict[str, Any], words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The anchor's text line plus the header-like lines directly above / below it (multi-line column labels)."""
    h = _h(anchor)
    x_min = float(anchor["bbox"][0]) - 0.8 * h
    x_max = float(anchor["bbox"][0]) + 80 * h
    near = [w for w in words if abs(_cy(w) - _cy(anchor)) <= _HEADER_BAND_HEIGHTS * h and x_min <= float(w["bbox"][0]) <= x_max]
    lines = cluster_rows(near, y_tolerance=0.4 * h)
    index = next((i for i, line in enumerate(lines) if any(w is anchor for w in line)), None)
    if index is None:
        return []
    anchor_line = lines[index]
    # restrict every line to the part right of the anchor that is not separated from it by a huge gap
    band = list(anchor_line)
    for step in (-1, 1):
        i = index + step
        prev_cy = sum(_cy(w) for w in anchor_line) / len(anchor_line)
        while 0 <= i < len(lines):
            line = lines[i]
            cy = sum(_cy(w) for w in line) / len(line)
            if abs(cy - prev_cy) > 1.9 * h or not _is_header_line(line):
                break
            band.extend(line)
            prev_cy = cy
            i += step
    return band


def _title_cut(anchor: dict[str, Any], words: list[dict[str, Any]]) -> float:
    """X where a neighbouring table starts: titles printed side by side above adjacent tables («Экспликация МОП»,
    «Спецификация квартир», ...) -- the first title start at/after the anchor is this table's own, the next is a cut."""
    h = _h(anchor)
    top = float(anchor["bbox"][1])
    starts = sorted(float(w["bbox"][0]) for w in words
                    if top - _TITLE_SEARCH_HEIGHTS * h <= _cy(w) < top and re.match(r"^(экспликац|спецификац|ведомост)", norm(w.get("text")))
                    and float(w["bbox"][0]) >= float(anchor["bbox"][0]) - 6 * h)
    distinct: list[float] = []
    for x in starts:
        if not distinct or x - distinct[-1] > 3 * h:
            distinct.append(x)
    return distinct[1] - 0.5 * h if len(distinct) > 1 else float("inf")


def _header_columns(anchor: dict[str, Any], words: list[dict[str, Any]]) -> tuple[list[Column], list[dict[str, Any]]] | None:
    """Columns of the header that starts at `anchor` («Номер»/«№»), or None when it is not a room table header."""
    h = _h(anchor)
    band = sorted(_header_band(anchor, words), key=lambda w: (float(w["bbox"][0]), _cy(w)))
    cut = _title_cut(anchor, words)
    taken: list[dict[str, Any]] = []
    right = float(anchor["bbox"][2])
    seen_content = False
    for word in band:
        if word is anchor:
            taken.append(word)
            continue
        x0 = float(word["bbox"][0])
        if x0 < float(anchor["bbox"][0]) - 0.8 * h:
            continue
        if x0 - right > _HEADER_MAX_GAP_HEIGHTS * h or x0 >= cut:
            break
        text = _norm_header(word.get("text"))
        if _is_number_header(word) and x0 > float(anchor["bbox"][2]) and seen_content and abs(_cy(word) - _cy(anchor)) < h:
            break                                            # the next side-by-side block starts here
        if any(ch.isdigit() for ch in text) and not re.search(r"(м2|м²|n)", text):
            continue                                         # a value/drawing number in the band, not a label
        taken.append(word)
        right = max(right, float(word["bbox"][2]))
        role = _column_role(text)
        if role in (ROLE_NAME, ROLE_TYPE, ROLE_AREA, ROLE_COUNT):
            seen_content = True
    if len(taken) < 2:
        return None
    # header words overlapping in X (over several lines) form one column
    intervals: list[list[Any]] = []
    for word in sorted(taken, key=lambda w: float(w["bbox"][0])):
        x0, x1 = float(word["bbox"][0]), float(word["bbox"][2])
        if intervals:
            last = intervals[-1]
            same_line_gap = min((x0 - float(m["bbox"][2]) for m in last[2] if abs(_cy(m) - _cy(word)) < 0.5 * h), default=None)
            other_line_overlap = any(abs(_cy(m) - _cy(word)) >= 0.5 * h and x0 < float(m["bbox"][2]) + 0.25 * h and x1 > float(m["bbox"][0]) - 0.25 * h
                                     for m in last[2])
            glued = same_line_gap is not None and same_line_gap <= 0.45 * h
            if other_line_overlap or glued:
                last[1] = max(last[1], x1)
                last[2].append(word)
                continue
        intervals.append([x0, x1, [word]])
    columns = []
    for x0, x1, members in intervals:
        label = " ".join(str(w["text"]) for w in sorted(members, key=lambda w: (round(_cy(w) / max(1.0, h)), float(w["bbox"][0]))))
        columns.append(Column(role=_column_role(label), label=label, x0=x0, x1=x1))
    if not columns or columns[0].role != ROLE_NUMBER:
        columns[0].role = ROLE_NUMBER
    roles = {c.role for c in columns}
    if not (roles & TEXT_ROLES) or not (roles & NUMERIC_ROLES):
        return None
    # merge a second NAME/OTHER label that is really a wrapped part of the name label («Наименование» / «помещения»)
    for i, col in enumerate(columns):
        lo = (columns[i - 1].x1 + col.x0) / 2 if i else col.x0 - 3.0 * h
        hi = (col.x1 + columns[i + 1].x0) / 2 if i + 1 < len(columns) else col.x1 + 3.0 * h
        col.span = (lo, hi)
    return columns, taken


# ---------------------------------------------------------------- scope


def parse_scope(text: str, *, source: str = "title") -> Scope:
    raw = norm(text)
    floor = None
    match = _FLOOR_NUMBER_RE.search(raw)
    if match:
        floor = re.sub(r"\s+", "", match.group(1)).replace("–", "-")
        floor = str(int(floor)) if floor.isdigit() else floor
    else:
        for regex, label in _FLOOR_WORDS:
            if regex.search(raw):
                floor = label
                break
    if floor is None and re.search(r"первого\s+этаж", raw):
        floor = "1"
    section = None
    match = _SECTION_RE.search(raw)
    if match:
        value = next(g for g in match.groups() if g)
        section = re.sub(r"^[сc]", "", value, flags=re.IGNORECASE)
        section = str(int(section)) if section.isdigit() else section.upper()
    building = None
    match = _BUILDING_RE.search(raw)
    if match:
        building = next(g for g in match.groups() if g).upper()
    qualifiers = tuple(label for label, regex in _QUALIFIERS if regex.search(raw))
    return Scope(floor=floor, section=section, building=building, qualifiers=qualifiers, source=source)


def _title_for(columns: list[Column], header_top: float, h: float, rows: list[list[dict[str, Any]]]) -> tuple[str, list[float] | None]:
    x_lo, x_hi = columns[0].x0 - 6 * h, columns[-1].x1 + 6 * h
    best: tuple[float, str, list[float]] | None = None
    for row in rows:
        cy = sum(_cy(w) for w in row) / len(row)
        if not (header_top - _TITLE_SEARCH_HEIGHTS * h <= cy < header_top):
            continue
        inside = [w for w in row if float(w["bbox"][2]) >= x_lo and float(w["bbox"][0]) <= x_hi]
        text = " ".join(str(w["text"]) for w in inside)
        if not inside or not _TITLE_RE.search(text):
            continue
        distance = header_top - cy
        if best is None or distance < best[0]:
            best = (distance, text, union_bbox([w["bbox"] for w in inside]))
    return (best[1], best[2]) if best else ("", None)


_PLAN_CAPTION_RE = re.compile(r"(план|маркировочн)", re.IGNORECASE)


def page_floor_captions(rows: list[list[dict[str, Any]]]) -> list[Scope]:
    scopes = []
    for row in rows:
        text = " ".join(str(w["text"]) for w in row)
        if _PLAN_CAPTION_RE.search(text) and "этаж" in norm(text):
            scope = parse_scope(text, source="page_caption")
            if scope.floor:
                scopes.append(scope)
    return scopes


# ---------------------------------------------------------------- rows


def _is_key(word: dict[str, Any]) -> bool:
    return bool(ROOM_KEY_RE.match(str(word.get("text") or "").strip()))


def _column_for(x: float, columns: list[Column]) -> int:
    for i, col in enumerate(columns):
        if col.span[0] <= x < col.span[1]:
            return i
    return 0 if x < columns[0].span[0] else len(columns) - 1


def _parse_block(columns: list[Column], header_words: list[dict[str, Any]], words: list[dict[str, Any]], h: float) -> tuple[list[Row], list[Total], float]:
    x_lo, x_hi = columns[0].span[0], columns[-1].span[1]
    header_bottom = max(float(w["bbox"][3]) for w in header_words)
    content_x = next((c.x0 for c in columns[1:] if c.role in TEXT_ROLES), columns[1].x0 if len(columns) > 1 else columns[0].x1 + 3 * h)
    below = sorted((w for w in words if _cy(w) > header_bottom and x_lo <= _cx(w) < x_hi and w not in header_words),
                   key=lambda w: (_cy(w), float(w["bbox"][0])))
    lines = cluster_rows(below)
    keys: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    totals: list[Total] = []
    last_y = header_bottom
    pitches: list[float] = []
    end_y = None
    for line in lines:
        line_cy = sum(_cy(w) for w in line) / len(line)
        first = _norm_header(line[0].get("text"))
        if first in _STAMP_WORDS or _TABLE_END_RE.match(" ".join(norm(w.get("text")) for w in line)):
            end_y = line_cy
            break
        if any(_TOTAL_WORD_RE.match(_norm_header(w.get("text"))) for w in line):
            values = [str(w["text"]) for w in line if parse_decimal(w.get("text")) is not None]
            label = " ".join(str(w["text"]) for w in line if parse_decimal(w.get("text")) is None and norm(w.get("text")) not in _UNIT_WORDS)
            totals.append(Total(label=label, values=values, bbox=union_bbox([w["bbox"] for w in line]) or []))
            end_y = line_cy
            break
        gap_limit = _MAX_ROW_GAP_PITCHES * (median(pitches) if pitches else 3.0 * h)
        if keys and line_cy - last_y > max(gap_limit, 2.5 * h):
            end_y = last_y + (median(pitches) if pitches else 1.5 * h) / 2
            break
        key_word = next((w for w in line if _is_key(w) and float(w["bbox"][0]) < content_x - 0.1 * h and _column_for(_cx(w), columns) == 0), None)
        if key_word is None and keys and _is_unlabelled_total(line, columns) and line_cy - last_y <= 2.2 * (median(pitches) if pitches else 2 * h):
            # a lone number under the area column right after the last row: the schedule's total without a label
            totals.append(Total(label="", values=[str(w["text"]) for w in line if parse_decimal(w.get("text")) is not None],
                                bbox=union_bbox([w["bbox"] for w in line]) or []))
            end_y = line_cy
            break
        apartment = _apartment_header(line)
        if apartment is not None:
            groups.append(apartment)                 # «Квартира 2» sub-header row: the rooms below belong to it
            last_y = line_cy
            continue
        if key_word is not None and len(line) == 1 and _GROUP_KEY_RE.match(str(key_word.get("text") or "").strip()):
            groups.append(key_word)                  # «1.1.2» alone on its line: the apartment the next rooms belong to
            last_y = line_cy
            continue
        if key_word is not None:
            if keys:
                pitches.append(_cy(key_word) - _cy(keys[-1]))
            keys.append(key_word)
            last_y = line_cy
        if len(keys) >= _MAX_ROWS:
            break
    keys = _aligned_keys(keys, h)
    if not keys:
        return [], totals, header_bottom
    pitches = [b - a for a, b in zip((_cy(k) for k in keys), (_cy(k) for k in keys[1:]))]
    pitch = median(pitches) if pitches else 2.0 * h
    if end_y is None:
        end_y = _cy(keys[-1]) + pitch / 2
    key_ids = {id(k) for k in keys}
    last_bottom = min(end_y, _cy(keys[-1]) + pitch / 2 + 0.5 * h)
    content = [w for w in below if id(w) not in key_ids and _cy(w) < last_bottom and _cy(w) > _cy(keys[0]) - 4 * pitch]
    text_spans = [c.span for c in columns if c.role in TEXT_ROLES]
    assigned = _assign_lines(keys, cluster_rows(content, y_tolerance=0.4 * h), header_bottom, h, text_spans)
    rows: list[Row] = []
    for i, key_word in enumerate(keys):
        members = assigned[i]
        cells: dict[int, list[dict[str, Any]]] = {}
        for word in members:
            index = _column_for(_cx(word), columns)
            if index == 0:
                index = 1 if len(columns) > 1 else 0
            cells.setdefault(index, []).append(word)
        text_cells = {}
        for index, cell_words in cells.items():
            ordered = sorted(cell_words, key=lambda w: (round(_cy(w) / max(1.0, 0.8 * h)), float(w["bbox"][0])))
            text_cells[index] = " ".join(str(w["text"]) for w in ordered)
        box = union_bbox([key_word["bbox"], *[w["bbox"] for w in members]]) or _box(key_word)
        key = str(key_word["text"]).strip()
        group = next((g for g in reversed(groups) if _cy(g) < _cy(key_word)), None)
        if group is not None and (str(group["text"]).startswith("кв. ") or not _GROUP_KEY_RE.match(key)):
            key = f"{str(group['text']).strip()} пом. {key}"       # room numbers restart in every apartment
        rows.append(Row(key=key, cells=text_cells, bbox=box, key_bbox=_box(key_word)))
    rows = _valid_rows(rows, columns)
    if not rows:
        return [], totals, header_bottom
    table_bottom = max([r.bbox[3] for r in rows] + [t.bbox[3] for t in totals if t.bbox])
    return rows, totals, table_bottom


def _apartment_header(line: list[dict[str, Any]]) -> dict[str, Any] | None:
    tokens = [norm(w.get("text")).strip(" .,:") for w in line]
    if len(tokens) in (2, 3) and tokens[0] in ("квартира", "кв") and re.match(r"^[\dа-я.\-]{1,8}$", tokens[1]) \
            and not any(parse_decimal(t) is not None for t in tokens[2:]):
        return {"text": f"кв. {str(line[1]['text']).strip()}", "bbox": union_bbox([w["bbox"] for w in line])}
    return None


def _is_unlabelled_total(line: list[dict[str, Any]], columns: list[Column]) -> bool:
    roles = [columns[_column_for(_cx(w), columns)].role for w in line if norm(w.get("text")) not in _UNIT_WORDS]
    numbers = [w for w in line if parse_decimal(w.get("text")) is not None]
    return bool(numbers) and len(roles) == len(numbers) and all(role in AREA_ROLES for role in roles)


def _aligned_keys(keys: list[dict[str, Any]], h: float) -> list[dict[str, Any]]:
    """Room numbers of one column are aligned (left, centre or right); a number far off that alignment is a
    drawing mark (dimension, axis, level) that happens to sit in the column's X range."""
    if len(keys) < 3:
        return keys
    refs = [median(float(k["bbox"][0]) for k in keys), median(_cx(k) for k in keys), median(float(k["bbox"][2]) for k in keys)]
    return [k for k in keys if min(abs(float(k["bbox"][0]) - refs[0]), abs(_cx(k) - refs[1]), abs(float(k["bbox"][2]) - refs[2])) <= 1.2 * h]


def _valid_rows(rows: list[Row], columns: list[Column]) -> list[Row]:
    """A row states something: a name/type text or a number in a numeric column. The table ends at the first two
    consecutive empty rows (drawing numbers below the schedule); a single empty row is dropped."""
    def valid(row: Row) -> bool:
        for index, text in row.cells.items():
            role = columns[index].role if index < len(columns) else ROLE_OTHER
            if role in TEXT_ROLES and re.search(r"[а-яa-z]{2,}", norm(text)):
                return True
            if role in NUMERIC_ROLES and any(parse_decimal(t) is not None for t in str(text).split()):
                return True
        return False

    out: list[Row] = []
    empty_run = 0
    for row in rows:
        if valid(row):
            out.append(row)
            empty_run = 0
            continue
        empty_run += 1
        if empty_run >= 2 and out:
            break
    return out


def _assign_lines(keys: list[dict[str, Any]], lines: list[list[dict[str, Any]]], header_bottom: float, h: float,
                  text_spans: list[tuple[float, float]] | None = None) -> list[list[dict[str, Any]]]:
    """Text lines of the block -> the row (key) they belong to.

    Two cell layouts exist: vertically CENTRED cells (the key sits in the middle of a wrapped name, so the lines of
    one row are symmetric around its key) and TOP-aligned cells (the key on the first line, the name wraps below).
    Centred is detected by text above the first key or a key whose own line carries no name text. Centred: a row
    takes the lines on its key line, the lines above it that the previous row did not take, and as many lines below
    it as it has above; top-aligned: every line down to the next key."""
    out: list[list[dict[str, Any]]] = [[] for _ in keys]
    if not keys:
        return out
    key_cy = [_cy(k) for k in keys]
    tol = 0.5 * h
    line_cy = [sum(_cy(w) for w in line) / len(line) for line in lines]
    on_key = [next((i for i, y in enumerate(key_cy) if abs(y - cy) <= tol), None) for cy in line_cy]
    above_first = any(on_key[j] is None and header_bottom < line_cy[j] < key_cy[0] - tol for j in range(len(lines)))
    def has_text(line: list[dict[str, Any]]) -> bool:
        return any(lo <= _cx(w) < hi for w in line for lo, hi in (text_spans or [(float("-inf"), float("inf"))]))

    empty_key_line = any(not any(on_key[j] == i and has_text(lines[j]) for j in range(len(lines))) for i in range(len(keys)))
    centred = above_first or empty_key_line
    between: list[list[int]] = [[] for _ in range(len(keys) + 1)]   # between[i] = lines above key i (between[-1] below last)
    for j, cy in enumerate(line_cy):
        if on_key[j] is not None:
            out[on_key[j]].extend(lines[j])
            continue
        slot = next((i for i, y in enumerate(key_cy) if cy < y), len(keys))
        between[slot].append(j)
    if not centred:
        for slot, members in enumerate(between):
            target = slot - 1
            if target >= 0:
                for j in members:
                    out[target].extend(lines[j])
        return out
    carry_above = [j for j in between[0] if line_cy[j] > header_bottom]
    for i in range(len(keys)):
        for j in carry_above:
            out[i].extend(lines[j])
        gap = between[i + 1]
        take = len(carry_above) if i + 1 < len(keys) else len(gap)
        mine, rest = gap[:take], gap[take:]
        for j in mine:
            out[i].extend(lines[j])
        carry_above = rest
    return out


# ---------------------------------------------------------------- entry point


def find_room_tables(snapshot: dict[str, Any], *, page: int | None = None) -> list[RoomTable]:
    """Every room / apartment schedule on one page snapshot, side-by-side blocks of one schedule merged."""
    words = dedupe_overprint(snapshot.get("words") or [])
    if not words:
        return []
    width = float(snapshot.get("width") or 0) or 1.0
    height = float(snapshot.get("height") or 0) or 1.0
    page = page if page is not None else snapshot.get("page")
    lines = cluster_rows(words)
    captions = page_floor_captions(lines)
    blocks: list[dict[str, Any]] = []
    used_anchor_ids: set[int] = set()
    for anchor in sorted((w for w in words if _is_number_header(w)), key=lambda w: (_cy(w), float(w["bbox"][0]))):
        if id(anchor) in used_anchor_ids:
            continue
        parsed = _header_columns(anchor, words)
        if parsed is None:
            continue
        columns, header_words = parsed
        used_anchor_ids.update(id(w) for w in header_words)
        h = _h(anchor)
        rows, totals, bottom = _parse_block(columns, header_words, words, h)
        if not rows:
            continue
        header_top = min(float(w["bbox"][1]) for w in header_words)
        title, title_bbox = _title_for(columns, header_top, h, lines)
        box = union_bbox([*[w["bbox"] for w in header_words], *[r.bbox for r in rows], *[t.bbox for t in totals if t.bbox]])
        if title_bbox:
            box = union_bbox([box, title_bbox])
        blocks.append({"columns": columns, "rows": rows, "totals": totals, "title": title, "title_bbox": title_bbox,
                       "bbox": box, "header_top": header_top, "h": h})
    tables: list[RoomTable] = []
    for block in _merge_side_by_side(blocks):
        title = block["title"]
        scope = parse_scope(title) if title else Scope(source="none")
        if scope.floor is None:
            floors = {c.floor for c in captions}
            if len(floors) == 1:
                caption = captions[0]
                scope = Scope(floor=caption.floor, section=scope.section or caption.section, building=scope.building or caption.building,
                              qualifiers=scope.qualifiers, source="page_caption")
        kind = _table_kind(title, block["columns"])
        if kind is None:
            continue
        tables.append(RoomTable(
            kind=kind, title=title, title_bbox=block["title_bbox"], scope=scope, columns=block["columns"], rows=block["rows"],
            totals=block["totals"], bbox=block["bbox"], page=page, width=width, height=height, blocks=block["blocks"],
        ))
    return tables


def _table_kind(title: str, columns: list[Column]) -> str | None:
    roles = {c.role for c in columns}
    text = norm(title)
    if title and not _TITLE_SUBJECT_RE.search(text) and "экспликац" not in text:
        return None                                   # «Спецификация дверей», «Ведомость отделки» ... are not room tables
    if _NOT_ROOM_SCHEDULE_RE.search(text):
        return None                                   # finish / floor / ceiling schedules list rooms but state other quantities
    if not (roles & AREA_ROLES) and not ({ROLE_COUNT, ROLE_ROOMS} & roles and "квартир" in text):
        return None
    if "квартир" in text and ("спецификац" in text or "ведомост" in text or roles & {ROLE_TYPE, ROLE_ROOMS, ROLE_COUNT}) and "помещен" not in text:
        return KIND_APARTMENTS
    return KIND_ROOMS


def _merge_side_by_side(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blocks with the same title text whose headers sit on the same line (a long schedule printed in columns),
    or a titleless block right of a titled one on the same header line, are one table."""
    out: list[dict[str, Any]] = []
    for block in sorted(blocks, key=lambda b: (round(b["header_top"] / max(1.0, b["h"] * 2)), b["bbox"][0])):
        prev = out[-1] if out else None
        if prev is not None and abs(prev["header_top"] - block["header_top"]) <= 1.5 * block["h"] \
                and prev["unit_roles"] == [c.role for c in block["columns"]] \
                and (block["title"] == prev["title"] or not block["title"] or not prev["title"]) \
                and not prev["totals"]:
            offset = len(prev["columns"])
            for row in block["rows"]:
                row.cells = {k + offset: v for k, v in row.cells.items()}
            prev["columns"] = prev["columns"] + block["columns"]
            prev["rows"] = prev["rows"] + block["rows"]
            prev["totals"] = prev["totals"] + block["totals"]
            prev["bbox"] = union_bbox([prev["bbox"], block["bbox"]])
            prev["title"] = prev["title"] or block["title"]
            prev["title_bbox"] = prev["title_bbox"] or block["title_bbox"]
            prev["blocks"] += 1
            continue
        out.append({**block, "blocks": 1, "unit_roles": [c.role for c in block["columns"]]})
    return out


# ---------------------------------------------------------------- cell accessors (used by the comparison)


def row_values(table: RoomTable, row: Row) -> dict[str, Any]:
    """{role: value} of one row; for merged side-by-side blocks the block's own columns are used. Numeric roles
    carry (first Decimal, all Decimals in the cell, raw text, decimals); text roles carry the text."""
    out: dict[str, Any] = {}
    for index, text in row.cells.items():
        if index >= len(table.columns):
            continue
        role = table.columns[index].role
        if role in NUMERIC_ROLES:
            tokens = [t for t in str(text).split() if norm(t) not in _UNIT_WORDS]
            numbers = [(parse_decimal(t), t) for t in tokens]
            numbers = [(value, raw) for value, raw in numbers if value is not None]
            if numbers and role not in out:
                out[role] = {"value": numbers[0][0], "all": [v for v, _ in numbers], "raw": " ".join(raw for _, raw in numbers),
                             "decimals": max(decimals_of(raw) for _, raw in numbers)}
        elif role in TEXT_ROLES and role not in out:
            out[role] = " ".join(str(text).split())
    return out


_GARBLED_CHAR_RE = re.compile(r"[\u0180-\u02af\ue000-\uf8ff\ufffd]")


def table_text_is_garbled(table: RoomTable, *, share: float = 0.02) -> bool:
    """A text layer whose font map is broken reads as Latin-Extended / private-use glyph soup («ǖестниȂная»);
    values read from it are not evidence (LOW_QUALITY, never compared)."""
    text = "".join([table.title, *(v for r in table.rows for v in r.cells.values()), *(r.key for r in table.rows)])
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and len(_GARBLED_CHAR_RE.findall(text)) / len(letters) >= share


def total_value(total: Total) -> tuple[Decimal | None, list[Decimal], int]:
    values = [v for v in (parse_decimal(t) for t in total.values) if v is not None]
    return (values[0] if values else None), values, max((decimals_of(t) for t in total.values), default=0)
