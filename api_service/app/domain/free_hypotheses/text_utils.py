"""Small text helpers shared by the page-text detectors (CHANGE_SHEET, LAYER_COMPOSITION).

Stems are the first five letters of a Cyrillic word of four letters or more (no dictionary, no morphology): enough to
tell "бронированный" from "бронированного" and cheap enough to index every page of a drawing set.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

_WORD_RE = re.compile(r"[а-яё]{4,}")
_PARENS_RE = re.compile(r"\([^)]*\)")
_ANALOG_RE = re.compile(r"\bили\s+аналог\w*", re.IGNORECASE)
STEM_LENGTH = 5


_LATIN_LOOKALIKES = str.maketrans("acekmopxy", "асекморху")
_TOKEN_RE = re.compile(r"\S+")


def fold(text: object) -> str:
    """Lower case, ё -> е, and Latin look-alike letters inside a word that also has Cyrillic letters mapped to Cyrillic
    (a CAD export sometimes prints "исключeн" with a Latin e)."""
    value = str(text or "").lower().replace("ё", "е")

    def fix(match: re.Match) -> str:
        token = match.group(0)
        return token.translate(_LATIN_LOOKALIKES) if re.search(r"[а-я]", token) else token

    return _TOKEN_RE.sub(fix, value)


def stems(text: object) -> list[str]:
    """Ordered, de-duplicated stems of the Cyrillic words in `text`."""
    seen: dict[str, None] = {}
    for word in _WORD_RE.findall(fold(text)):
        seen.setdefault(word[:STEM_LENGTH], None)
    return list(seen)


def clean_name(text: object) -> str:
    """A printed material / element name without remarks in brackets and "или аналог"."""
    value = _PARENS_RE.sub(" ", str(text or ""))
    value = _ANALOG_RE.sub(" ", value)
    return " ".join(value.split()).strip(" ,;:-–—")


def join_words(words: Iterable[dict[str, Any]], *, y_tolerance: float = 3.0) -> str:
    """Words in reading order (lines by y, words by x) as one string."""
    from ..anchor_search import cluster_rows

    rows = cluster_rows(list(words), y_tolerance=y_tolerance)
    return " ".join(" ".join(str(w.get("text") or "") for w in row) for row in rows).strip()


def union_box(words: Iterable[dict[str, Any]]) -> list[float] | None:
    boxes = [w["bbox"] for w in words if w.get("bbox")]
    if not boxes:
        return None
    return [round(min(b[0] for b in boxes), 2), round(min(b[1] for b in boxes), 2), round(max(b[2] for b in boxes), 2), round(max(b[3] for b in boxes), 2)]
