"""Sheet metadata for pairing: cipher / sheet number / title / kind / floor / room numbers / text labels of ONE page.

Everything is read from the text layer of the page (nothing is invented; a page without a text layer yields only geometry).
The cipher and sheet number reuse `inspector_card.extract_sheet_designation` (the card already shows them); rooms and floor reuse
`room_drawing_compare` (step A). The result is a pure function of the page content, cached per SHA-256 of the file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..free_hypotheses.text_utils import fold, stems
from .config import MODEL_VERSION

logger = logging.getLogger(__name__)

_TITLE_X = 0.45
_TITLE_Y = 0.70
_LABEL_WORDS = {
    "изм", "кол", "уч", "лист", "листов", "док", "подп", "дата", "разраб", "разработал", "пров", "проверил", "нач", "отд", "н.контр",
    "гип", "гл", "стадия", "утв", "формат", "копировал", "инв", "взам", "подпись", "ип", "гап", "зав", "рук", "нормоконтроль", "кол.уч",
    "№док", "№", "ооо", "ао", "мм", "м", "стр",
}
_KIND_RULES = (
    ("plan", re.compile(r"\bплан\w*|\bпланы\b|\bразбивоч\w+\s+план|\bсхем\w+\s+расположени")),
    ("section", re.compile(r"\bразрез\w*|\bсечени\w*")),
    ("facade", re.compile(r"\bфасад\w*")),
    ("scheme", re.compile(r"\bсхем\w*|\bаксонометр\w*|\bпринципиальн\w*")),
    ("unit", re.compile(r"\bузел\b|\bузлы\b|\bузлов\b|\bдетал\w*|\bразвертк\w*")),
    ("spec", re.compile(r"\bспецификац\w*|\bведомост\w*|\bэкспликац\w*|\bтаблиц\w*|\bперечень\b|\bкаталог\w*")),
    ("general", re.compile(r"\bобщие\s+данные|\bобщие\s+указани|\bусловн\w+\s+обозначени|\bпояснит\w+\s+записк")),
)
_CAPTION_START = re.compile(r"^(?:план|планы|разрез|разрезы|сечение|фасад|фасады|схема|узел|узлы|развертк|аксонометр|принципиальн|экспликац|спецификац|ведомост|общие\s+данные)")
DRAWING_KINDS = {"plan", "section", "facade", "scheme", "unit"}
_ROOM_TYPE_KINDS = {"plan"}
_LEVEL_RE = re.compile(r"(?:отм\w*\.?\s*)([+\-±]?\d{1,3}[.,]\d{3})")
_TOKEN_RE = re.compile(r"[a-zа-я0-9][a-zа-я0-9.\-]{2,}")


def visible_words(page) -> list[tuple[float, float, float, float, str]]:
    """Words of the text layer in the visible (rotated) frame -- the same frame bbox_pdf and the renderer use."""
    from ..inspector_card import _visible_words

    return _visible_words(page)


def _lines(words: list[tuple[float, float, float, float, str]]) -> list[tuple[float, float, str, list]]:
    """Reading-order lines: (y_top, x_left, text, words)."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows: list[list] = []
    for word in ordered:
        height = max(1.0, word[3] - word[1])
        centre = (word[1] + word[3]) / 2
        if rows and abs(rows[-1][0] - centre) <= height * 0.45:
            rows[-1][1].append(word)
            rows[-1][0] = (rows[-1][0] * (len(rows[-1][1]) - 1) + centre) / len(rows[-1][1])
        else:
            rows.append([centre, [word]])
    out = []
    for centre, row in rows:
        row.sort(key=lambda w: w[0])
        out.append((min(w[1] for w in row), row[0][0], " ".join(w[4] for w in row), row))
    return out


def sheet_kind(text: str) -> str | None:
    folded = fold(text)
    for kind, pattern in _KIND_RULES:
        if pattern.search(folded):
            return kind
    return None


def _is_label_line(text: str) -> bool:
    tokens = [t.strip(".,:;()") for t in fold(text).split() if t.strip(".,:;()")]
    return not tokens or sum(1 for t in tokens if t in _LABEL_WORDS) >= max(1, len(tokens) // 2)


_NAME_RE = re.compile(r"^[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ]\.?\s?[А-ЯЁ]?\.?)?$")


def _dedupe_words(text: str) -> str:
    """A CAD export often prints a caption twice in the same place ("Н. Н. контр. контр."): consecutive repeats collapse."""
    out: list[str] = []
    for word in re.sub(r"\s+", " ", text).strip().split(" "):
        if not out or out[-1] != word:
            out.append(word)
    return " ".join(out)


def extract_title(words: list[tuple[float, float, float, float, str]], width: float, height: float) -> dict[str, Any]:
    """The sheet title: the title-block lines that carry a drawing-kind word ("План 1 этажа", "Разрез 1-1"), else the page lines
    that do. Returns {"title", "kind", "source"}; the title is None when the page names nothing."""
    block = [w for w in words if w[0] > width * _TITLE_X and w[3] > height * _TITLE_Y]
    block_lines = [ln for ln in _lines(block) if not _is_label_line(ln[2]) and not _NAME_RE.match(ln[2].strip())]
    titled = [ln for ln in block_lines if sheet_kind(ln[2]) and len(ln[2]) <= 140]
    if titled:
        text = " ".join(ln[2] for ln in titled[:3])
        return {"title": _dedupe_words(text), "kind": sheet_kind(text), "source": "title_block"}
    # the big caption under the drawing ("План на отм. +3.000") sits outside the stamp
    rest = [w for w in words if not (w[0] > width * _TITLE_X and w[3] > height * _TITLE_Y)]
    captions = [ln for ln in _lines(rest) if _CAPTION_START.match(fold(ln[2])) and 6 <= len(ln[2]) <= 110 and len(ln[2].split()) <= 14]
    if captions:
        captions.sort(key=lambda ln: (-(ln[3][0][3] - ln[3][0][1]), ln[0], ln[1]))   # the biggest lettering first
        text = captions[0][2]
        return {"title": _dedupe_words(text), "kind": sheet_kind(text), "source": "page_caption"}
    # no drawing-kind word anywhere: the sheet has no readable title (note lists / stamps are not titles); pairing then rests on
    # the cipher, rooms and labels, with the lower confidence that follows
    return {"title": None, "kind": None, "source": None}


def stamp_stems(words: list[tuple[float, float, float, float, str]], width: float, height: float) -> list[str]:
    """Stems of the descriptive text of the title block (the name of the document / system / sheet; labels and names of the
    signatories dropped). What is printed on every sheet of the document (address, organisation) is removed later, per document, by
    `annotate_document`."""
    block = [w for w in words if w[0] > width * _TITLE_X and w[3] > height * _TITLE_Y]
    lines = [ln for ln in _lines(block) if not _is_label_line(ln[2]) and not _NAME_RE.match(ln[2].strip())]
    return sorted({s for ln in lines for s in stems(ln[2])})[:80]


BOILERPLATE_SHARE = 0.35
BOILERPLATE_MIN_PAGES = 6


def annotate_document(pages: dict[int, dict[str, Any]]) -> None:
    """Sets `stamp_distinct` on every page: the title-block stems that are NOT printed on a third or more of the document's
    pages (the project address, the author organisation and the stage are on all of them and say nothing about the sheet)."""
    stamped = [m for m in pages.values() if m.get("stamp_stems")]
    counts: dict[str, int] = {}
    for m in stamped:
        for s in m["stamp_stems"]:
            counts[s] = counts.get(s, 0) + 1
    limit = max(2, BOILERPLATE_SHARE * len(stamped)) if len(stamped) >= BOILERPLATE_MIN_PAGES else len(stamped) + 1
    for m in pages.values():
        m["stamp_distinct"] = [s for s in m.get("stamp_stems") or [] if counts.get(s, 0) < limit]


def _label_set(words: list[tuple[float, float, float, float, str]], width: float, height: float) -> list[str]:
    """Normalised text labels of the drawing area (title block excluded): Cyrillic stems and marks (П1, ВЕ3, Т1, ...)."""
    keep: set[str] = set()
    for x0, y0, x1, y1, text in words:
        if x0 > width * _TITLE_X and y1 > height * _TITLE_Y:
            continue
        token = fold(text).strip(".,:;()[]")
        if len(token) < 2 or not _TOKEN_RE.fullmatch(token):
            continue
        if token.isdigit():
            continue
        if re.fullmatch(r"[а-я]{4,}", token):
            keep.add(token[:5])
        elif re.search(r"[a-zа-я]", token) and re.search(r"\d", token) and len(token) <= 12:
            keep.add(token)
    return sorted(keep)


def page_meta(page, *, width: float | None = None, height: float | None = None) -> dict[str, Any]:
    """Metadata of one fitz page (visible frame)."""
    from ..inspector_card import extract_sheet_designation
    from .. import room_drawing_compare as rdc

    rect = page.rect
    width = float(width or rect.width)
    height = float(height or rect.height)
    words = visible_words(page)
    meta: dict[str, Any] = {
        "width": round(width, 2), "height": round(height, 2), "words": len(words),
        "has_text": len(words) >= 12,
    }
    designation = extract_sheet_designation(words, width, height) or {}
    meta["sheet_no"] = designation.get("sheet")
    meta["cipher"] = designation.get("cipher")
    title = extract_title(words, width, height)
    meta.update({"title": title["title"], "kind": title["kind"], "title_source": title["source"], "stamp_stems": stamp_stems(words, width, height)})
    snapshot = {"words": [{"text": w[4], "bbox": [w[0], w[1], w[2], w[3]]} for w in words], "height": height,
                "text": " ".join(w[4] for w in words)}
    context = rdc.detect_sheet_context(snapshot)
    meta["floor"] = context.get("floor")
    meta["floor_status"] = context.get("floor_status")
    meta["building"] = context.get("building")
    meta["building_status"] = context.get("building_status")
    rooms = sorted({row["room"] for row in rdc.extract_room_words(snapshot)}) if meta["kind"] in _ROOM_TYPE_KINDS or meta["kind"] is None else []
    meta["rooms"] = rooms[:400]
    meta["levels"] = sorted({m.group(1).replace(",", ".") for m in _LEVEL_RE.finditer(fold(snapshot["text"]))})[:40]
    meta["labels"] = _label_set(words, width, height)[:600]
    return meta


# ---------------------------------------------------------------------------------------------------------------
# cache per SHA-256 of the file
# ---------------------------------------------------------------------------------------------------------------
def cache_dir() -> Path | None:
    explicit = os.getenv("CASE10_SHEET_COMPARE_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    tagger = os.getenv("CASE10_LIVE_TAGGER_CACHE_DIR", "").strip()
    return Path(tagger) / "sheet_compare" if tagger else None


def code_fingerprint() -> str:
    digest = hashlib.sha256(MODEL_VERSION.encode())
    for name in ("meta.py",):
        digest.update((Path(__file__).parent / name).read_bytes())
    return digest.hexdigest()[:12]


_FP: str | None = None


def _fp() -> str:
    global _FP
    if _FP is None:
        _FP = code_fingerprint()
    return _FP


def document_meta(pdf_bytes: bytes, sha256: str | None = None, *, pages: range | list[int] | None = None) -> dict[int, dict[str, Any]]:
    """{page_number: meta} for the requested 1-based pages (default all), cached per file hash."""
    import fitz

    sha = sha256 or hashlib.sha256(pdf_bytes).hexdigest()
    directory = cache_dir()
    path = directory / "meta" / f"{sha}.json" if directory else None
    stored: dict[str, Any] = {}
    if path is not None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("fp") == _fp():
                stored = record.get("pages") or {}
        except (OSError, ValueError):
            stored = {}
    out: dict[int, dict[str, Any]] = {}
    dirty = False
    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        wanted = list(pages) if pages is not None else list(range(1, len(pdf) + 1))
        for number in wanted:
            if number < 1 or number > len(pdf):
                continue
            item = stored.get(str(number))
            if item is None:
                try:
                    item = page_meta(pdf[number - 1])
                except Exception as exc:  # noqa: BLE001 -- a damaged page must not stop the scan
                    logger.warning("sheet meta failed for page %s: %s", number, exc)
                    item = {"error": "unreadable"}
                stored[str(number)] = item
                dirty = True
            out[number] = item
    if dirty and path is not None:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"fp": _fp(), "pages": stored}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("sheet meta cache write failed: %s", exc)
    # boilerplate of the title block is judged over every page scanned so far (the cache holds them), not only the requested ones
    everything = {int(k): dict(v) for k, v in stored.items() if isinstance(v, dict)}
    annotate_document(everything)
    return {number: everything.get(number, item) for number, item in out.items()}


def title_stems(meta: dict[str, Any]) -> list[str]:
    return stems(meta.get("title") or "")


def is_drawing(meta: dict[str, Any]) -> bool:
    """A sheet worth rastering: it names a drawing kind in its title, or it is a large-format sheet (A2 and bigger)."""
    if meta.get("error"):
        return False
    if meta.get("kind") in DRAWING_KINDS:
        return True
    if meta.get("kind") is not None:          # specification / schedule / legend / general data: a table, not a drawing
        return False
    return max(float(meta.get("width") or 0), float(meta.get("height") or 0)) >= 1000
