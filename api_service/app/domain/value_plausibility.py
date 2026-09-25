"""Word-level "is this number a measurement of the right physical kind?" facts (Phase 10, prompt B, 2(в)).

The generic anchor mechanisms take "the nearest number after the label". Real pages give back clause
numbers ("п 7.19"), list markers ("8."), dates ("04.25") and heights where a volume was asked for
("Предельная высота 40,95 м" for a м³ parameter). Each of those is a NON-comparison: the SILVER review of
19 committed groups found 22 of 38 cited values were not a genuine statement of the parameter.

This module computes, at extraction time (where the word list and the number's word span are known),
whether the number is backed by a unit of the parameter's kind:
  * `ADJACENT`  the unit token is the word right before/after the number ("88264,00 м3", "м² 25036,27");
  * `IN_ROW`    the unit appears elsewhere on the label's visual row (unit column / "Площадь, м²");
  * `IN_COLUMN` the unit appears in the column ABOVE the number (a table whose header says "Количество, м³" and
                whose rows carry bare numbers -- the usual shape of a ведомость земляных масс / благоустройства);
  * `MISMATCH`  the word right next to the number is a DIFFERENT physical unit (м next to a м³ parameter);
  * `ABSENT`    no unit of that kind anywhere in the row and none adjacent  -> not a measurement;
  * `NOT_APPLICABLE` the parameter has no physical unit (counts, categorical codes).

Pure functions over the word list, stdlib only.
"""
from __future__ import annotations

import re
from typing import Any, Sequence

ADJACENT = "ADJACENT"
IN_ROW = "IN_ROW"
IN_COLUMN = "IN_COLUMN"
MISMATCH = "MISMATCH"
ABSENT = "ABSENT"
NOT_APPLICABLE = "NOT_APPLICABLE"

# unit family -> accepted surface tokens (already normalised: lower-case, "²"->"2", "³"->"3", no dots)
_FAMILY_TOKENS: dict[str, frozenset[str]] = {
    "LENGTH_M": frozenset({"м", "m", "пм"}),
    "LENGTH_MM": frozenset({"мм", "mm"}),
    "AREA_M2": frozenset({"м2", "m2", "квм", "м2)", "кв"}),
    "AREA_MM2": frozenset({"мм2"}),
    "AREA_HA": frozenset({"га"}),
    "VOLUME_M3": frozenset({"м3", "m3", "кубм", "куб"}),
    "PERCENT": frozenset({"%", "проц"}),
    "PERMILLE": frozenset({"‰"}),
}
_OTHER_UNIT_TOKENS = frozenset({
    "т", "кг", "шт", "штук", "компл", "ед", "чел", "дн", "дней", "сут", "мин", "ч", "квт", "вт", "гкал", "па",
    "а", "ом", "руб", "тыс", "л", "лс", "м3/ч", "м3/сут", "гкал/ч", "квтч", "°", "град", "мес",
})
_TOKEN_FAMILY: dict[str, str] = {tok: fam for fam, toks in _FAMILY_TOKENS.items() for tok in toks}
_DIMENSION = {"LENGTH_M": "LENGTH", "LENGTH_MM": "LENGTH", "AREA_M2": "AREA", "AREA_MM2": "AREA", "AREA_HA": "AREA",
              "VOLUME_M3": "VOLUME", "PERCENT": "RATIO", "PERMILLE": "RATIO"}
_CATALOG_UNIT_FAMILY = {"м": "LENGTH_M", "мм": "LENGTH_MM", "м2": "AREA_M2", "м3": "VOLUME_M3", "%": "PERCENT",
                        "мм2": "AREA_MM2", "‰": "PERMILLE", "га": "AREA_HA"}
_STRIP = " \t\r\n.,;:()[]«»\"'“”„=*"
_PUNCT_ONLY = re.compile(r"^[\-–—:=/]+$")


def normalize_unit_token(text: object) -> str:
    token = str(text or "").lower().replace("ё", "е").replace("²", "2").replace("³", "3").replace("^2", "2").replace("^3", "3")
    token = token.strip(_STRIP).replace(".", "")
    return token


def catalog_unit_family(unit: object) -> str | None:
    """Physical family of a catalog `unit` cell ("м²" -> AREA_M2); None for counts, codes and compounds."""
    return _CATALOG_UNIT_FAMILY.get(normalize_unit_token(unit))


def token_unit_family(token: object) -> str | None:
    norm = normalize_unit_token(token)
    if norm in _TOKEN_FAMILY:
        return _TOKEN_FAMILY[norm]
    if norm in _OTHER_UNIT_TOKENS:
        return "OTHER"
    return None


# How far left/right of the number a header word may sit and still belong to its column: header cells are often centred
# over a right-aligned number column, so the unit token can be offset by up to half a typical column width.
_COLUMN_X_MARGIN = 40.0


def column_text_above(words: Sequence[dict[str, Any]], first_idx: int, last_idx: int, *, x_margin: float = _COLUMN_X_MARGIN) -> str:
    """Text of the words ABOVE the number (same page) whose horizontal centre lies within the number's x-range widened
    by `x_margin` -- i.e. the number's own column, header included. Empty when the words carry no bounding boxes."""
    boxes = [w.get("bbox") for w in words[first_idx:last_idx + 1] if w.get("bbox")]
    if not boxes:
        return ""
    x0 = min(b[0] for b in boxes) - x_margin
    x1 = max(b[2] for b in boxes) + x_margin
    top = min(b[1] for b in boxes)
    picked = []
    for word in words:
        box = word.get("bbox")
        if not box or box[3] > top + 0.5:
            continue
        centre = (box[0] + box[2]) / 2
        if x0 <= centre <= x1:
            picked.append((box[1], box[0], str(word.get("text") or "")))
    return " ".join(text for _y, _x, text in sorted(picked))


def _significant(words: Sequence[dict[str, Any]], start: int, step: int) -> tuple[int, dict[str, Any]] | None:
    """The nearest non-punctuation word from `start` in direction `step`."""
    i = start
    while 0 <= i < len(words):
        text = str(words[i].get("text") or "").strip()
        if text and not _PUNCT_ONLY.match(text):
            return i, words[i]
        i += step
    return None


def _unit_at(words: Sequence[dict[str, Any]], index: int, step: int) -> str | None:
    """Unit family of the word at `index`, understanding the two-word spellings "кв. м" / "куб. м"."""
    hit = _significant(words, index, step)
    if hit is None:
        return None
    pos, word = hit
    family = token_unit_family(word.get("text"))
    if family is None:
        return None
    norm = normalize_unit_token(word.get("text"))
    neighbour = _significant(words, pos + (1 if step > 0 else -1), 1 if step > 0 else -1)
    neighbour_norm = normalize_unit_token(neighbour[1].get("text")) if neighbour else ""
    if norm == "кв" and neighbour_norm == "м" and step > 0:
        return "AREA_M2"
    if norm == "м" and step < 0 and neighbour_norm == "кв":
        return "AREA_M2"
    if norm == "куб" and neighbour_norm == "м" and step > 0:
        return "VOLUME_M3"
    if norm == "м" and step < 0 and neighbour_norm == "куб":
        return "VOLUME_M3"
    if norm in {"кв", "куб"}:
        return "AREA_M2" if norm == "кв" else "VOLUME_M3"
    return family


def _has_family(text: str | None, expected: str) -> bool:
    tokens = {normalize_unit_token(part) for part in str(text or "").split()}
    expected_tokens = _FAMILY_TOKENS[expected] - {"кв", "куб"}
    return bool(tokens & expected_tokens) or (expected == "AREA_M2" and "кв" in tokens and "м" in tokens) or (
        expected == "VOLUME_M3" and "куб" in tokens and "м" in tokens
    )


def unit_evidence(
    words: Sequence[dict[str, Any]], first_idx: int, last_idx: int, expected_unit: object, *, row_text: str | None = None,
    column_text: str | None = None,
) -> dict[str, Any]:
    """Unit facts for the number spanning words[first_idx..last_idx]. `column_text` defaults to the words above the
    number in its own column (`column_text_above`)."""
    expected = catalog_unit_family(expected_unit)
    if expected is None:
        return {"verdict": NOT_APPLICABLE, "expected_family": None}
    after = _unit_at(words, last_idx + 1, 1)
    before = _unit_at(words, first_idx - 1, -1)
    found, side = (after, "after") if after else ((before, "before") if before else (None, None))
    if found is not None:
        same_dimension = _DIMENSION.get(found) == _DIMENSION.get(expected) and found != "OTHER"
        if found == expected:
            verdict = ADJACENT
        elif same_dimension:
            verdict = ADJACENT  # same physical dimension, different scale (мм vs м): the trigger evaluator handles the scale
        else:
            verdict = MISMATCH
        return {"verdict": verdict, "expected_family": expected, "adjacent_family": found, "adjacent_side": side}
    if _has_family(row_text, expected):
        return {"verdict": IN_ROW, "expected_family": expected, "adjacent_family": None, "adjacent_side": None}
    column = column_text if column_text is not None else column_text_above(words, first_idx, last_idx)
    if _has_family(column, expected):
        return {"verdict": IN_COLUMN, "expected_family": expected, "adjacent_family": None, "adjacent_side": None}
    return {"verdict": ABSENT, "expected_family": expected, "adjacent_family": None, "adjacent_side": None}


def component_unit_evidence(
    words: Sequence[dict[str, Any]], first_idx: int, last_idx: int, label: object, *, row_text: str | None = None,
) -> dict[str, Any]:
    """Compound components carry a catalog label ("м³", "т", "шт.", "Ом"): the literal label must sit next to
    the number, somewhere on its row, or in its column above. Labels that are physical families reuse `unit_evidence`."""
    if catalog_unit_family(label) is not None:
        return unit_evidence(words, first_idx, last_idx, label, row_text=row_text)
    wanted = normalize_unit_token(label)
    if not wanted:
        return {"verdict": NOT_APPLICABLE, "expected_family": None}
    for step, index in ((1, last_idx + 1), (-1, first_idx - 1)):
        hit = _significant(words, index, step)
        if hit and normalize_unit_token(hit[1].get("text")) == wanted:
            return {"verdict": ADJACENT, "expected_family": wanted}
    if wanted in {normalize_unit_token(part) for part in str(row_text or "").split()}:
        return {"verdict": IN_ROW, "expected_family": wanted}
    if wanted in {normalize_unit_token(part) for part in column_text_above(words, first_idx, last_idx).split()}:
        return {"verdict": IN_COLUMN, "expected_family": wanted}
    return {"verdict": ABSENT, "expected_family": wanted}


def backed(evidence: dict[str, Any] | None) -> bool:
    """True when the unit evidence does not contradict the number being a measurement of the parameter."""
    return not evidence or evidence.get("verdict") in {ADJACENT, IN_ROW, IN_COLUMN, NOT_APPLICABLE}
