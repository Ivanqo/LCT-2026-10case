"""CHANGE_SHEET -- a change the working documentation (RD) records about itself that the project documentation (PD) still
shows in its earlier state (TZ 9.5; the organizer's examples: "бронированный лист исключён из состава покрытия",
"в квартире 2.4.1 подвинулась дверь в помещении СУ").

Two stages, both pure functions of page text (no object constants, no page numbers, no file names):

1. `parse_change_pages` -- finds the change register of an RD sheet by its header band («Изм.» «Лист» «Содержание изменения»
   [«Код» «Примечание»]) and reads it entry by entry: the sheet-number cell of a row is the entry marker, the content column
   words belong to the marker nearest in Y (an entry is vertically centred on its marker, a long text wraps above and below it).
   The title block of the sheet (Разрешение / Обозначение / Наименование объекта) sits inside the page in some layouts and is
   cut out by its own vocabulary. The result is a list of entries: revision, sheet, text, code, box.
2. `detect` -- for each entry that states a change of something existing (excluded, removed, moved, replaced, changed) looks for
   the same element in the PD of the same discipline: either an identifier the entry names (apartment / room number such as 2.4.1,
   a mark such as ПР-49) that occurs in few PD pages together with other words of the entry, or a window of one-two text lines
   that holds the rare words of the entry. No match in the PD -> no hypothesis (a difference is never claimed without a PD source).
   "Added" entries are not used: the absence of something in the PD cannot be shown from its text.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .. import table_parser as tpar
from ..anchor_search import cluster_rows
from .text_utils import clean_name, fold, join_words, stems, union_box

DETECTOR = "change_sheet.rd_change_vs_pd"

KIND_REMOVED, KIND_MOVED, KIND_REPLACED, KIND_CHANGED, KIND_ADDED = "REMOVED", "MOVED", "REPLACED", "CHANGED", "ADDED"
_KIND_RE = (
    (KIND_REMOVED, re.compile(r"(?<![а-я])(исключ|удал[ие]н|удалят|демонтир|убра|упразднен|ликвидир)")),
    (KIND_MOVED, re.compile(r"(?<![а-я])(перенес|перенос|подвин|смещ|перемещ|сдвин)")),
    (KIND_REPLACED, re.compile(r"(?<![а-я])(замен[аеёиыу]|заменен|заменит)")),
    (KIND_ADDED, re.compile(r"(?<![а-я])(добавл|введен[ыао]?(?![а-я])|дополнен)")),
    (KIND_CHANGED, re.compile(r"(?<![а-я])(измен|откорректир|скорректир|исправ|уточн|уменьш|увелич|переименов)")),
)
# words of a register entry that name the act of changing or the register itself, not the changed element
_VERB_STEMS = frozenset({
    "исклю", "удале", "демон", "убран", "упраз", "ликви", "перен", "подви", "смеще", "перем", "сдвин", "замен", "добав", "введе", "допол",
    "измен", "откор", "скорр", "испра", "уточн", "умень", "увели", "переи", "внесе", "учтен", "указа", "преду", "выпол",
    "котор", "данны", "связи", "также", "после", "перед", "более", "менее", "только", "всех", "всей", "была", "были", "будет",
})
_ID_DOTTED_RE = re.compile(r"(?<![\d.,-])\d{1,3}(?:\.\d{1,3}){2,3}(?![\d.]*\d)(?![-–]\d)")
_ID_MARK_RE = re.compile(r"(?<![А-Яа-яA-Za-z0-9])[А-ЯA-Z]{1,5}[-–]\d{1,3}[а-яa-z]?(?![\dА-Яа-яA-Za-z])")
_SHEET_CELL_RE = re.compile(r"^\d{1,3}(?:[-–,]\d{1,3})*$")
_IZM_CELL_RE = re.compile(r"^\d{1,2}$")
_HEADER_CONTENT_RE = re.compile(r"^(содержани|описани)")
_TITLE_BLOCK_WORDS = frozenset({"разрешение", "обозначение", "наименование", "объекта", "строительства"})
MAX_MARKER_DISTANCE = 30.0


def entry_kind(text: str) -> str | None:
    folded = fold(text)
    for kind, pattern in _KIND_RE:
        if pattern.search(folded):
            return kind
    return None


def entry_ids(text: str) -> list[str]:
    found = _ID_DOTTED_RE.findall(text or "") + [m.replace("–", "-") for m in _ID_MARK_RE.findall(text or "")]
    return list(dict.fromkeys(found))


# drawing-set language that names a kind of sheet or document, not the changed element ("на планах", "схема", "спецификация"):
# a PD line sharing only these words with a register entry is not the same element
_GENERIC_STEMS = frozenset({
    "плана", "схема", "схему", "схемы", "схем", "специ", "систе", "узел", "узлы", "узле", "разре", "ведом", "черте", "табли", "данны", "общие",
    "общей", "текст", "сечен", "фрагм", "компл", "приме", "марка", "марки", "услов", "обозн", "значе", "колич", "эскиз", "рабоч", "проек", "докум",
    "секци", "жилой", "части", "часть", "связи", "листа", "листы", "листе", "форма", "формы", "полож", "наиме",
    "назва", "номер", "номера", "объек", "этаж", "этажа", "этажи", "этаже", "плане",
})


def entry_subject_stems(text: str) -> list[str]:
    return [s for s in stems(text) if s not in _VERB_STEMS and s not in _GENERIC_STEMS]


# ------------------------------------------------------------------------------------------------ 1. the register


def _norm(word: dict[str, Any]) -> str:
    return fold(word.get("text")).strip(" .,;:()")


def _find_header(rows: list[list[dict[str, Any]]]) -> dict[str, Any] | None:
    """Columns of a change register from its header band; None when the page has no such header."""
    for row in rows:
        for index, word in enumerate(row):
            if not _HEADER_CONTENT_RE.match(_norm(word)):
                continue
            nxt = row[index + 1] if index + 1 < len(row) else None
            if nxt is None or not _norm(nxt).startswith("изменени"):
                continue
            cy = (word["bbox"][1] + word["bbox"][3]) / 2
            band = [w for r in rows for w in r if abs((w["bbox"][1] + w["bbox"][3]) / 2 - cy) <= 12]
            izm = next((w for w in band if _norm(w).startswith("изм") and w["bbox"][2] < word["bbox"][0]), None)
            sheet = next((w for w in band if _norm(w).startswith("лист") and w["bbox"][2] < word["bbox"][0]), None)
            if izm is None or sheet is None:
                continue
            code = next((w for w in band if _norm(w) in ("код", "кодт") and w["bbox"][0] > nxt["bbox"][2]), None)
            note = next((w for w in band if _norm(w).startswith("примеч") and w["bbox"][0] > nxt["bbox"][2]), None)
            right = min([w["bbox"][0] for w in (code, note) if w is not None] or [10 ** 6])
            return {
                "y": cy, "bottom": max(w["bbox"][3] for w in band),
                "izm": (izm["bbox"][0] - 14, (izm["bbox"][2] + sheet["bbox"][0]) / 2),
                "sheet": ((izm["bbox"][2] + sheet["bbox"][0]) / 2, sheet["bbox"][2] + 6),
                "content": (sheet["bbox"][2] + 6, right - 3),
                "code": (code["bbox"][0] - 8, (code["bbox"][2] + (note["bbox"][0] if note else code["bbox"][2] + 60)) / 2) if code else None,
            }
    return None


def _title_block_band(rows: list[list[dict[str, Any]]], header_y: float) -> tuple[float, float] | None:
    """Y range of the sheet's own title block (Разрешение / Обозначение / Наименование объекта / строительства) when it is printed
    on the register page: anchored on the row holding both «Разрешение» and «Обозначение», about 100 pt tall."""
    for row in rows:
        names = {_norm(w) for w in row}
        if {"разрешение", "обозначение"} <= names:
            top = row[0]["bbox"][1] - 4
            bottom = top + 104
            if top < header_y:
                bottom = min(bottom, header_y - 8)
            return top, bottom
    return None


def parse_change_register(snapshot: dict[str, Any], *, page: int | None = None) -> list[dict[str, Any]]:
    words = tpar.dedupe_overprint(snapshot.get("words") or [])
    if len(words) < 8:
        return []
    rows = cluster_rows(words, y_tolerance=3)
    header = _find_header(rows)
    if header is None:
        return []
    band = _title_block_band(rows, header["y"])
    sheet_lo, sheet_hi = header["sheet"]
    izm_lo, izm_hi = header["izm"]
    c_lo, c_hi = header["content"]
    markers: list[dict[str, Any]] = []
    content: list[dict[str, Any]] = []
    codes: list[dict[str, Any]] = []
    for word in words:
        x0, y0, x1, y1 = word["bbox"]
        cy = (y0 + y1) / 2
        if abs(cy - header["y"]) <= 12 and y1 <= header["bottom"] + 1:
            continue
        if band and band[0] <= cy <= band[1]:
            continue
        text = str(word.get("text") or "").strip()
        centre = (x0 + x1) / 2
        if sheet_lo <= centre <= sheet_hi and _SHEET_CELL_RE.match(text):
            markers.append(word)
        elif c_lo <= x0 and x1 <= c_hi + 2 and not (sheet_lo <= centre <= sheet_hi and _SHEET_CELL_RE.match(text)):
            content.append(word)
        elif header["code"] and header["code"][0] <= centre <= header["code"][1] and re.match(r"^\d{1,2}$", text):
            codes.append(word)
    if not markers or not content:
        return []
    markers.sort(key=lambda w: (w["bbox"][1] + w["bbox"][3]) / 2)
    entries: list[dict[str, Any]] = []
    for marker in markers:
        my = (marker["bbox"][1] + marker["bbox"][3]) / 2
        izm_word = next((w for w in words if izm_lo <= (w["bbox"][0] + w["bbox"][2]) / 2 <= izm_hi and abs((w["bbox"][1] + w["bbox"][3]) / 2 - my) <= 4
                         and _IZM_CELL_RE.match(str(w.get("text") or "").strip())), None)
        entries.append({"marker": marker, "y": my, "izm": str(izm_word["text"]).strip() if izm_word else None, "words": [], "izm_word": izm_word})
    for word in content:
        cy = (word["bbox"][1] + word["bbox"][3]) / 2
        nearest = min(entries, key=lambda e: abs(e["y"] - cy))
        if abs(nearest["y"] - cy) <= MAX_MARKER_DISTANCE:
            nearest["words"].append(word)
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not entry["words"]:
            continue
        text = join_words(entry["words"])
        boxed = list(entry["words"]) + [entry["marker"]] + ([entry["izm_word"]] if entry["izm_word"] else [])
        code = next((str(c["text"]) for c in codes if abs((c["bbox"][1] + c["bbox"][3]) / 2 - entry["y"]) <= 14), None)
        out.append({
            "page": page if page is not None else snapshot.get("page"), "izm": entry["izm"], "sheet": str(entry["marker"]["text"]).strip(),
            "text": text, "code": code, "bbox": union_box(boxed), "width": float(snapshot.get("width") or 0), "height": float(snapshot.get("height") or 0),
        })
    return out


# ------------------------------------------------------------------------------------------------ 2. the PD side


def stem_weights(subject: list[str], page_frequency: dict[str, int], total_pages: int) -> dict[str, float]:
    """Inverse page frequency of each stem of an entry in the PD pages of the discipline (rarer = heavier)."""
    weights = {s: math.log((total_pages + 1) / (page_frequency.get(s, 0) + 1)) + 0.1 for s in subject}
    # a word the PD never prints (the RD says "металлический", the PD says "стальной") cannot be required of a match: half weight
    return {s: (w * 0.5 if page_frequency.get(s, 0) == 0 else w) for s, w in weights.items()}


MAX_WINDOW_WORDS = 9                 # a label / legend line / table row of the PD; running text is not a counterpart
MAX_ID_WINDOW_WORDS = 40


def best_window(words: list[dict[str, Any]], weights: dict[str, float], ids: list[str], *, max_words: int = MAX_WINDOW_WORDS) -> dict[str, Any] | None:
    """Best one-or-two-line window of a PD page for an entry: weighted share of the entry's stems it holds. A window that holds
    one of the entry's identifiers gets credit for it. Returns text, box, matched stems, coverage."""
    rows = cluster_rows(tpar.dedupe_overprint(words), y_tolerance=3)
    total = sum(weights.values()) or 1.0
    folded_ids = {fold(i) for i in ids}
    best = None
    for index, row in enumerate(rows):
        candidates = [row]
        if index + 1 < len(rows) and abs(rows[index + 1][0]["bbox"][0] - row[0]["bbox"][0]) <= 60 and rows[index + 1][0]["bbox"][1] - row[0]["bbox"][3] < 18:
            candidates.append(row + rows[index + 1])
        for window in candidates:
            if len(window) > max_words:                       # a sentence of running text is not an element label
                continue
            text = " ".join(str(w.get("text") or "") for w in window)
            found = set(stems(text)) & set(weights)
            id_hit = bool(folded_ids & {fold(str(w.get("text") or "").strip(" ,;:()")) for w in window})
            if not found and not id_hit:
                continue
            if folded_ids and not id_hit:                      # an entry that names an identifier is looked up by it
                continue
            covered = sum(weights[s] for s in found) / total
            score = covered + (0.6 if id_hit else 0.0)
            if best is None or score > best["score"]:
                best = {"score": score, "coverage": covered, "stems": sorted(found), "id_hit": id_hit, "text": " ".join(text.split()),
                        "bbox": union_box(window), "words": window}
    return best
