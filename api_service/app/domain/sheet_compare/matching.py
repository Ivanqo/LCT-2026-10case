"""Pairing of sheets of different stages (PD <-> RD) and of different revisions of one stage (RD <-> RD).

Three things are kept apart everywhere: the pair confidence computed here, the picture similarity (`raster.py`) and the
deviation (never computed). The pair confidence is a ranking score built from the evidence the two sheets share:

  title / kind      the sheet title ("План типового этажа на отм. +61.350 секция С1"): stems + numbers, optionally a
                    multilingual-MiniLM embedding (semantic_similarity.py), reported as `title_method`;
  floor / section   a floor, section, building or axes range named differently on the two sheets REJECTS the pair --
                    coinciding numbers of different floors or sections are never joined (experts' reference, item 2);
  rooms             the overlap of the room-number sets (step A: room_drawing_compare), used only when both sheets list >= 3 rooms;
  labels            the overlap of the text marks of the drawing area;
  cipher            the document cipher + sheet number (decisive only for the revisions of one document series).

A score from fewer than two substantive pieces of evidence is capped; a pair whose best candidate does not clearly lead the
runner-up is flagged `ambiguous` instead of being assigned silently. The weights are conventions, not fitted to a labelled example.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from ..free_hypotheses.text_utils import fold, stems
from .config import AMBIGUITY_MARGIN, MIN_PAIR_CONFIDENCE

WEIGHTS = {"title": 3.0, "levels": 2.5, "rooms": 2.5, "labels": 2.0, "cipher": 2.5, "kind": 1.0, "format": 1.0}
SUBSTANTIVE = ("title", "levels", "rooms", "labels", "cipher")
EVIDENCE_CAP = 0.45

_FLOOR_RE = re.compile(r"(-?\d{1,2})\s*-?\s*(?:й|го|ого|ом|ый)?\s*этаж")
_FLOOR_WORD = re.compile(r"(подвал|цоколь|кровл|чердак|техническ\w*\s+этаж|техэтаж|типов\w+\s+этаж)")
_SECTION_RE = re.compile(r"(?:секци[яиюе]|корпус\w*|блок[\s-]*секци\w*|литер\w*|подъезд\w*)\s*(?:№|n)?\s*([а-яa-z]\d{1,2}|\d{1,2}[а-яa-z]?|[а-яa-z])(?![а-яa-z0-9])")
_AXES_RE = re.compile(r"в\s+осях\s+([0-9а-яa-z/]+\s*[-–—]\s*[0-9а-яa-z/]+)")
_LEVEL_TITLE_RE = re.compile(r"отм\w*\.?\s*([+\-±]?\d{1,3}[.,]\d{3})")


def identity(title: str | None) -> dict[str, str]:
    """Floor / section / axes / level the title names: whatever is named differently on the two sheets forbids the pairing."""
    text = fold(title or "")
    out: dict[str, str] = {}
    match = _FLOOR_RE.search(text)
    if match:
        out["floor"] = str(int(match.group(1)))
    else:
        word = _FLOOR_WORD.search(text)
        if word:
            out["floor"] = re.sub(r"\s+", " ", word.group(1))[:5]
    section = _SECTION_RE.search(text)
    if section:
        out["section"] = section.group(1)
    axes = _AXES_RE.search(text)
    if axes:
        out["axes"] = re.sub(r"[\s–—]+", "", axes.group(1).replace("–", "-"))
    level = _LEVEL_TITLE_RE.search(text)
    if level:
        out["level"] = level.group(1).replace(",", ".").replace("±", "+")
    return out


def _tokens(title: str | None) -> set[str]:
    text = fold(title or "")
    tokens = set(stems(text))
    tokens.update(re.findall(r"(?<![\d.])\d{1,3}(?![\d])", text))
    tokens.update(m.replace(",", ".") for m in re.findall(r"[+\-±]?\d{1,3}[.,]\d{3}", text))
    return tokens


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def _normalise_cipher(value: str | None) -> str:
    return re.sub(r"[^0-9a-zа-я]", "", fold(value or ""))


_title_cos_cache: dict[tuple[str, str], float | None] = {}


def title_cosine(a: str, b: str) -> float | None:
    """Cosine of the sentence embeddings of two titles (semantic_similarity.py), None when the model is not available."""
    key = (a, b) if a <= b else (b, a)
    if key not in _title_cos_cache:
        from .. import semantic_similarity as sem

        value = sem.similarity(a, b) if sem.is_enabled() else None
        _title_cos_cache[key] = None if value is None else max(0.0, min(1.0, float(value)))
    return _title_cos_cache[key]


def _format_component(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    try:
        ra = float(a["width"]) / float(a["height"])
        rb = float(b["width"]) / float(b["height"])
    except (KeyError, TypeError, ZeroDivisionError):
        return None
    diff = abs(ra - rb) / max(ra, rb)
    return 1.0 if diff <= 0.05 else (0.5 if diff <= 0.12 else 0.0)


def _reject(reason: str) -> dict[str, Any]:
    return {"confidence": 0.0, "components": {}, "reject": reason, "basis": [], "evidence": 0, "title_method": None}


def score_pair(a: dict[str, Any], b: dict[str, Any], *, same_series: bool = False, use_embeddings: bool = True) -> dict[str, Any]:
    """`a`, `b` are page metadata (meta.page_meta). Returns {"confidence", "components", "reject", "basis"}."""
    ida, idb = identity(a.get("title")), identity(b.get("title"))
    for field, reason in (("floor", "разные этажи"), ("section", "разные секции/корпуса"), ("axes", "разные оси")):
        if field in ida and field in idb and ida[field] != idb[field]:
            return _reject(f"{reason}: {ida[field]} ≠ {idb[field]}")
    if a.get("floor") and b.get("floor") and a.get("floor_status") == "found" and b.get("floor_status") == "found":
        from ..room_drawing_compare import floors_relation

        if floors_relation(a["floor"], b["floor"]) == "different":
            return _reject(f"разные этажи: {a['floor']} ≠ {b['floor']}")
    if a.get("building") and b.get("building") and a["building"] != b["building"]:
        return _reject(f"разные секции/корпуса: {a['building']} ≠ {b['building']}")
    la, lb = a.get("levels") or [], b.get("levels") or []
    components: dict[str, float] = {}
    basis: list[str] = []
    ta, tb = a.get("title"), b.get("title")
    title_method = None
    if ta and tb:
        base = _jaccard(_tokens(ta), _tokens(tb))
        value = base
        title_method = "stems"
        if use_embeddings and base >= 0.2:
            cos = title_cosine(ta, tb)
            if cos is not None:
                value = 0.5 * base + 0.5 * cos
                title_method = "stems+embedding"
        components["title"] = round(value, 4)
        basis.append(f"название: «{ta}» ~ «{tb}»")
    if la and lb and len(la) <= 3 and len(lb) <= 3:
        shared = set(la) & set(lb)
        if not shared and a.get("kind") == "plan" and b.get("kind") == "plan":
            return _reject(f"разные отметки: {', '.join(la)} ≠ {', '.join(lb)}")
        components["levels"] = 1.0 if shared else 0.0
        if shared:
            basis.append(f"общая отметка {sorted(shared)[0]}")
    ra, rb = set(a.get("rooms") or []), set(b.get("rooms") or [])
    if len(ra) >= 3 and len(rb) >= 3:
        shared = ra & rb
        components["rooms"] = round(len(shared) / min(len(ra), len(rb)) * (0.5 + 0.5 * _jaccard(ra, rb)), 4) if len(shared) >= 3 else round(len(shared) / 10, 4)
        basis.append(f"общих номеров помещений: {len(shared)} (на листах {len(ra)} и {len(rb)})")
    sa, sb = set(a.get("labels") or []), set(b.get("labels") or [])
    if len(sa) >= 8 and len(sb) >= 8:
        components["labels"] = round(_jaccard(sa, sb), 4)
        basis.append(f"общих текстовых меток: {len(sa & sb)}")
    ca, cb = _normalise_cipher(a.get("cipher")), _normalise_cipher(b.get("cipher"))
    if same_series and ca and cb:
        if ca == cb:
            same_sheet = bool(a.get("sheet_no")) and a.get("sheet_no") == b.get("sheet_no")
            components["cipher"] = 1.0 if same_sheet else 0.35
            basis.append(f"шифр {a['cipher']}" + (f", лист {a['sheet_no']}" if same_sheet else ""))
        else:
            components["cipher"] = 0.0
    if a.get("kind") and b.get("kind"):
        components["kind"] = 1.0 if a["kind"] == b["kind"] else 0.0
    fmt = _format_component(a, b)
    if fmt is not None:
        components["format"] = fmt
    total = sum(WEIGHTS[k] for k in components)
    confidence = sum(WEIGHTS[k] * v for k, v in components.items()) / total if total else 0.0
    evidence = sum(1 for k in SUBSTANTIVE if k in components)
    if evidence < 2:
        confidence = min(confidence, EVIDENCE_CAP)
    return {"confidence": round(confidence, 4), "components": components, "reject": None, "basis": basis, "evidence": evidence, "title_method": title_method}


def match_sheets(left: list[dict[str, Any]], right: list[dict[str, Any]], *, same_series: bool = False, use_embeddings: bool = True,
                 min_confidence: float | None = None) -> list[dict[str, Any]]:
    """`left` / `right`: [{"key": any, "meta": page_meta}]. Mutual-best assignment; each pair carries its runner-up margin.
    Returns [{"left", "right", "confidence", "raw_confidence", "components", "ambiguous", "margin", "basis", "title_method"}]."""
    floor = MIN_PAIR_CONFIDENCE if min_confidence is None else min_confidence
    scores: dict[tuple[int, int], dict[str, Any]] = {}
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            result = score_pair(a["meta"], b["meta"], same_series=same_series, use_embeddings=False)
            if result["reject"] is None and result["confidence"] > 0:
                scores[(i, j)] = result
    if use_embeddings:
        for key, result in list(scores.items()):
            if result["confidence"] >= floor - 0.2:
                scores[key] = score_pair(left[key[0]]["meta"], right[key[1]]["meta"], same_series=same_series, use_embeddings=True)
    best_left: dict[int, list[tuple[float, int]]] = {}
    best_right: dict[int, list[tuple[float, int]]] = {}
    for (i, j), result in scores.items():
        best_left.setdefault(i, []).append((result["confidence"], j))
        best_right.setdefault(j, []).append((result["confidence"], i))
    for table in (best_left, best_right):
        for rows in table.values():
            rows.sort(key=lambda row: (-row[0], row[1]))
    pairs = []
    for i, rows in best_left.items():
        conf, j = rows[0]
        if best_right[j][0][1] != i or conf < floor:
            continue
        runner = max([r[0] for r in rows[1:2]] + [r[0] for r in best_right[j][1:2]] + [0.0])
        margin = conf - runner
        ambiguous = margin < AMBIGUITY_MARGIN
        result = scores[(i, j)]
        pairs.append({"left": left[i]["key"], "right": right[j]["key"], "confidence": round(conf * (0.85 if ambiguous else 1.0), 4),
                      "raw_confidence": conf, "components": result["components"], "ambiguous": ambiguous, "margin": round(margin, 4),
                      "basis": result["basis"], "title_method": result.get("title_method"), "evidence": result.get("evidence", 0)})
    pairs.sort(key=lambda p: (-p["confidence"], str(p["left"]), str(p["right"])))
    return [p for p in pairs if p["confidence"] >= floor]
