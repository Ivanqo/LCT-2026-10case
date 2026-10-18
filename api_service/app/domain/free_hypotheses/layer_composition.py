"""LAYER_COMPOSITION -- the layer build-up of a roof / floor / wall / slab as printed in the PD and in the RD (TZ 9.5; the
organizer's example: the armoured steel sheet of a roof pie is listed in the PD section and missing in the RD one).

1. `parse_compositions` (pure, per page): a composition is a vertical stack of at least three "layer rows" -- a material name
   followed by a thickness (`Пароизоляция   0`, `Цем.-песчаная стяжка 50`, `Утеплитель 100 мм`, `Плитка ... 10`). The thickness is
   the last number of a row segment; it stands right after the name or in a column of its own several columns to the right (then
   it must align with other numbers of that column). A name that wraps onto the next line is joined. A stack is accepted only if
   at least two of its rows carry a word of the construction-material vocabulary, so a column of "room - number" pairs is not one.
2. `compare_compositions`: a PD stack and an RD stack are the same construction if most of their layers pair up in order (the
   material names agree after dropping remarks, or are semantically close by the multilingual MiniLM embedding). Each unpaired
   layer is "removed" (PD only) / "added" (RD only) / "changed material" (both sides leave a layer at the same place and the
   two are not alike), and each paired layer with a different thickness (after unit normalisation) is "thickness changed".
   An RD stack that is identical to some PD stack is never reported; an RD stack is compared only with its closest PD stack.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import table_parser as tpar
from ..anchor_search import cluster_rows
from .text_utils import clean_name, fold, stems, union_box

DETECTOR = "layer_composition.pd_rd"

KIND_REMOVED, KIND_ADDED, KIND_MATERIAL, KIND_THICKNESS = "LAYER_REMOVED", "LAYER_ADDED", "MATERIAL_CHANGED", "THICKNESS_CHANGED"
MIN_LAYERS = 3
MAX_DIFFS = 2
MAX_THICKNESS_MM = Decimal(3000)
# construction-material vocabulary: used to tell a layer list from any other "name number" column, never as a list of what a layer is
_MATERIAL_RE = re.compile(
    r"(бетон|стяжк|утеплит|ват[аыу]|пенополи|пенопласт|мембран|плитк|клей|клее|пароизол|гидроизол|штукатур|кирпич|гипсокартон|гкл|ламинат|линолеум|"
    r"керамогранит|песчан|грунт|профнастил|пленк|плёнк|битум|щебен|геотекстил|экструз|минерал|фанер|осб|стеклоткан|подготовк|засыпк|керамзит|"
    r"облицовк|сэндвич|перлит|гравий|раствор|мастик|рубероид|покрыти|звукоизол|теплоизол|ограждающ|слой|пеноблок|газобетон|кладк|"
    r"сетк|армир|праймер|гидрофоб|профил|обшивк|пирог|воздушн|зазор|прослойк|ж/б|ж\.б|железобет|плит[аыуе]\b|перекрыт|стен[аыу]\b|стальн|стал[ьи]\b|металл|алюмин|бронир|нержав|оцинк)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"^\d{1,4}(?:[.,]\d{1,2})?$")
_UNIT_NEXT = {"мм": Decimal(1), "см": Decimal(10), "м": Decimal(1000)}
_STOP_NEXT = re.compile(r"^(м2|м²|%|шт|кг|кн|кпа|мпа|т|градус|°|кв|м3|м³|мм2|мм²|л|n)$", re.IGNORECASE)
_EXPLICIT_RE = re.compile(r"(?:\b[tтδd]\s*=\s*|(?<![\d.,=-]))(\d{1,4}(?:[.,]\d)?)\s*мм\b\.?", re.IGNORECASE)
_SEGMENT_GAP = 60.0
_LEFT_ALIGN = 12.0
_ROW_GAP = 30.0
_CONTINUATION_GAP = 15.0


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def _segments(row: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    for word in sorted(row, key=lambda w: w["bbox"][0]):
        if out and word["bbox"][0] - out[-1][-1]["bbox"][2] <= _SEGMENT_GAP:
            out[-1].append(word)
        else:
            out.append([word])
    return out


def _unit(token: str) -> str:
    return token.lower().strip(".,;)(")


def _is_thickness_cell(texts: list[str]) -> bool:
    """A segment that is only a thickness with its unit: `50 мм`, `- 50 мм`, `– 8 мм,`."""
    tokens = [t for t in texts if t not in ("-", "–", "—")]
    return len(tokens) == 2 and bool(_NUMBER_RE.match(tokens[0])) and _unit(tokens[1]) in _UNIT_NEXT


def _is_name(text: str) -> bool:
    return len(re.findall(r"[А-Яа-яЁё]{3,}", text)) >= 1


def _layer_rows(rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Every row segment that reads as "<name> <thickness>"."""
    numeric_columns: list[tuple[float, float]] = []                       # (x0, y) of lone numeric segments
    for row in rows:
        for seg in _segments(row):
            if _NUMBER_RE.match(str(seg[-1].get("text") or "").strip()):
                numeric_columns.append((seg[-1]["bbox"][0], (seg[-1]["bbox"][1] + seg[-1]["bbox"][3]) / 2))
    found: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        segs = _segments(row)
        for index, seg in enumerate(segs):
            texts = [str(w.get("text") or "").strip() for w in seg]
            last = texts[-1]
            thickness_word = None
            name_words: list[dict[str, Any]] = []
            unit_factor = Decimal(1)
            if len(seg) >= 2 and _NUMBER_RE.match(last) and _is_name(" ".join(texts[:-1])):
                thickness_word, name_words = seg[-1], seg[:-1]
            elif len(seg) >= 3 and _unit(texts[-1]) in _UNIT_NEXT and _NUMBER_RE.match(texts[-2]) and _is_name(" ".join(texts[:-2])):
                thickness_word, name_words, unit_factor = seg[-2], seg[:-2], _UNIT_NEXT[_unit(texts[-1])]
            elif index >= 1 and _is_thickness_cell(texts) and _is_name(" ".join(str(w.get("text") or "") for w in segs[index - 1])):
                number = next(w for w, t in zip(seg, texts) if _NUMBER_RE.match(t))
                thickness_word, name_words, unit_factor = number, segs[index - 1], _UNIT_NEXT[_unit(texts[-1])]
            elif len(seg) == 1 and _NUMBER_RE.match(last) and index >= 1 and _is_name(" ".join(str(w.get("text") or "") for w in segs[index - 1])):
                cy = (seg[0]["bbox"][1] + seg[0]["bbox"][3]) / 2
                aligned = sum(1 for x0, y in numeric_columns if abs(x0 - seg[0]["bbox"][0]) <= 4 and 0 < abs(y - cy) <= 90)
                if aligned >= 1:
                    thickness_word, name_words = seg[0], segs[index - 1]
            if thickness_word is None:
                continue
            nxt = segs[index + 1][0] if index + 1 < len(segs) and segs[index + 1] else None
            if nxt is not None and nxt["bbox"][0] - thickness_word["bbox"][2] <= 6 and _STOP_NEXT.match(str(nxt.get("text") or "").strip()):
                continue
            value = _dec(str(thickness_word.get("text")).strip())
            if value is None:
                continue
            if "," in str(thickness_word.get("text")) or "." in str(thickness_word.get("text")):
                if unit_factor == 1 and not (nxt is not None and str(nxt.get("text") or "").strip().lower() == "мм" and nxt["bbox"][0] - thickness_word["bbox"][2] <= 8):
                    continue                                              # a decimal without a unit is an area / a level, not a thickness
            mm = value * unit_factor
            if nxt is not None and nxt["bbox"][0] - thickness_word["bbox"][2] <= 8:
                unit = _unit(str(nxt.get("text") or ""))
                if unit in _UNIT_NEXT:
                    mm = value * _UNIT_NEXT[unit]
            if mm > MAX_THICKNESS_MM:
                continue
            name = " ".join(str(w.get("text") or "") for w in name_words)
            found.append({
                "y": (thickness_word["bbox"][1] + thickness_word["bbox"][3]) / 2, "x_left": name_words[0]["bbox"][0], "name_words": list(name_words),
                "thickness_word": thickness_word, "name": name, "mm": mm, "row_index": row_index,
            })
    return found


def _continuation(rows: list[list[dict[str, Any]]], layer: dict[str, Any], next_y: float | None) -> list[dict[str, Any]]:
    """Name words printed on the following line(s) of the same text block, left-aligned with the layer name, before the next layer row."""
    words: list[dict[str, Any]] = []
    y_ref = max(w["bbox"][3] for w in layer["name_words"])
    limit = layer["thickness_word"]["bbox"][2] + 80
    for row in rows[layer["row_index"] + 1: layer["row_index"] + 5]:
        block = [w for w in row if layer["x_left"] - _LEFT_ALIGN <= w["bbox"][0] <= limit]
        if not block:
            continue
        top = min(w["bbox"][1] for w in block)
        if top - y_ref > _CONTINUATION_GAP:
            break
        if next_y is not None and (top + max(w["bbox"][3] for w in block)) / 2 >= next_y - 2:
            break
        first = min(block, key=lambda w: w["bbox"][0])
        if abs(first["bbox"][0] - layer["x_left"]) > _LEFT_ALIGN:
            break
        text = " ".join(str(w.get("text") or "") for w in block)
        if re.search(r"\d\s*мм|^\s*\(?\s*сто\b|^\s*\d", text, re.IGNORECASE) or not _is_name(text):
            break
        head = re.sub(r"^[^А-Яа-яЁё]+", "", text)
        if head[:1].isupper() and not head.split()[0].isupper():      # "Мембрана ..." opens a layer of its own (printed without a thickness)
            break
        words.extend(block)
        y_ref = max(w["bbox"][3] for w in block)
    return words


def parse_compositions(snapshot: dict[str, Any], *, page: int | None = None) -> list[dict[str, Any]]:
    words = tpar.dedupe_overprint(snapshot.get("words") or [])
    if len(words) < 12:
        return []
    rows = cluster_rows(words, y_tolerance=3)
    layers = _layer_rows(rows)
    if len(layers) < MIN_LAYERS:
        return []
    layers.sort(key=lambda l: (round(l["x_left"] / _LEFT_ALIGN), l["y"]))
    stacks: list[list[dict[str, Any]]] = []
    for layer in sorted(layers, key=lambda l: l["y"]):
        placed = False
        for stack in stacks:
            tail = stack[-1]
            if abs(layer["x_left"] - tail["x_left"]) <= _LEFT_ALIGN and 0 < layer["y"] - tail["y"] <= _ROW_GAP:
                stack.append(layer)
                placed = True
                break
        if not placed:
            stacks.append([layer])
    out: list[dict[str, Any]] = []
    width, height = float(snapshot.get("width") or 0), float(snapshot.get("height") or 0)
    for stack in stacks:
        if len(stack) < MIN_LAYERS or sum(1 for l in stack if _MATERIAL_RE.search(l["name"])) < 2:
            continue
        items = []
        for index, layer in enumerate(stack):
            next_y = stack[index + 1]["y"] if index + 1 < len(stack) else None
            extra = _continuation(rows, layer, next_y)
            raw_name = clean_name(" ".join([layer["name"]] + [str(w.get("text") or "") for w in extra]))
            mm = layer["mm"]
            explicit = _EXPLICIT_RE.findall(raw_name)
            if explicit:                                      # "t=100мм" printed in the layer text beats a number of the neighbouring column
                value = _dec(explicit[0])
                if value is not None and value <= MAX_THICKNESS_MM:
                    mm = value
                name = " ".join(_EXPLICIT_RE.sub(" ", raw_name).split()).strip(" ,;:-–—(")
            else:
                name = raw_name
            if not name:
                continue
            items.append({"name": name, "mm": str(mm.normalize()) if mm != mm.to_integral() else str(int(mm)),
                          "bbox": union_box(layer["name_words"] + [layer["thickness_word"]] + extra)})
        if len(items) < MIN_LAYERS:
            continue
        top = min(l["y"] for l in stack)
        label = None
        for word in words:
            text = str(word.get("text") or "").strip()
            if re.match(r"^(?:[А-ЯA-Z]{1,3}[-–]?\d{1,2}|\d{1,2}\.\d{1,2})$", text) and abs(word["bbox"][0] - stack[0]["x_left"]) <= 24 and 0 < top - word["bbox"][3] <= 22:
                label = text
        out.append({"page": page if page is not None else snapshot.get("page"), "label": label, "layers": items, "width": width, "height": height,
                    "bbox": union_box([{"bbox": i["bbox"]} for i in items if i.get("bbox")]),
                    # every word stem printed on the page: a layer "missing" from a stack that the other side still names elsewhere
                    # on its page (printed without a thickness, in running text) is not a removed layer
                    "page_stems": sorted(set(stems(" ".join(str(w.get("text") or "") for w in words))))[:1500]})
    return out


# ------------------------------------------------------------------------------------------------ comparing


def layer_key(name: str) -> frozenset[str]:
    return frozenset(stems(name))


def names_alike(a: str, b: str, *, semantic_min: float, use_semantic: bool = True) -> tuple[bool, float]:
    """(same material, score). Shared stems first; for names that share none the embedding cosine decides."""
    ka, kb = layer_key(a), layer_key(b)
    if ka and kb:
        jac = len(ka & kb) / len(ka | kb)
        if jac >= 0.34 or (len(ka & kb) >= 1 and min(len(ka), len(kb)) <= 2 and (ka <= kb or kb <= ka)):
            return True, max(jac, 0.5)
    else:
        jac = 0.0
    if not use_semantic:
        return False, jac
    from .. import semantic_similarity as sem

    sim = sem.similarity(clean_name(a), clean_name(b)) if sem.is_enabled() else None
    if sim is None:
        return False, jac
    return sim >= semantic_min, float(sim)


def align_layers(pd_layers: list[dict[str, Any]], rd_layers: list[dict[str, Any]], *, semantic_min: float, use_semantic: bool = True) -> list[tuple[int | None, int | None, float]]:
    """Order-preserving alignment (LCS over name likeness) -> list of (pd index | None, rd index | None, score)."""
    n, m = len(pd_layers), len(rd_layers)
    sim = [[names_alike(pd_layers[i]["name"], rd_layers[j]["name"], semantic_min=semantic_min, use_semantic=use_semantic) for j in range(m)] for i in range(n)]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            take = best[i + 1][j + 1] + (1.0 + sim[i][j][1] * 0.01) if sim[i][j][0] else -1.0
            best[i][j] = max(best[i + 1][j], best[i][j + 1], take)
    out: list[tuple[int | None, int | None, float]] = []
    i = j = 0
    while i < n and j < m:
        if sim[i][j][0] and best[i][j] == best[i + 1][j + 1] + (1.0 + sim[i][j][1] * 0.01):
            out.append((i, j, sim[i][j][1]))
            i += 1
            j += 1
        elif best[i + 1][j] >= best[i][j + 1]:
            out.append((i, None, 0.0))
            i += 1
        else:
            out.append((None, j, 0.0))
            j += 1
    out.extend((k, None, 0.0) for k in range(i, n))
    out.extend((None, k, 0.0) for k in range(j, m))
    return out


def compare_compositions(pd: dict[str, Any], rd: dict[str, Any], *, semantic_min: float, use_semantic: bool = True) -> dict[str, Any]:
    """Alignment of two compositions with the differences found. `paired` counts layers present on both sides."""
    aligned = align_layers(pd["layers"], rd["layers"], semantic_min=semantic_min, use_semantic=use_semantic)
    paired = [(i, j) for i, j, _ in aligned if i is not None and j is not None]
    diffs: list[dict[str, Any]] = []
    index = 0
    while index < len(aligned):
        i, j, _ = aligned[index]
        if i is not None and j is not None:
            a, b = Decimal(pd["layers"][i]["mm"]), Decimal(rd["layers"][j]["mm"])
            if a != b:
                diffs.append({"kind": KIND_THICKNESS, "pd": i, "rd": j, "pd_mm": str(a), "rd_mm": str(b)})
            index += 1
            continue
        # an unpaired run: a PD layer and an RD layer next to each other with nothing paired between them is a replaced material
        run = []
        while index < len(aligned) and (aligned[index][0] is None or aligned[index][1] is None):
            run.append(aligned[index])
            index += 1
        pd_run = [i for i, j, _ in run if i is not None]
        rd_run = [j for i, j, _ in run if j is not None]
        pairs = min(len(pd_run), len(rd_run))
        for k in range(pairs):
            diffs.append({"kind": KIND_MATERIAL, "pd": pd_run[k], "rd": rd_run[k]})
        for i in pd_run[pairs:]:
            diffs.append({"kind": KIND_REMOVED, "pd": i, "rd": None})
        for j in rd_run[pairs:]:
            diffs.append({"kind": KIND_ADDED, "pd": None, "rd": j})
    return {"paired": len(paired), "diffs": diffs, "aligned": aligned}


def same_construction(result: dict[str, Any], pd: dict[str, Any], rd: dict[str, Any]) -> bool:
    shortest = min(len(pd["layers"]), len(rd["layers"]))
    longest = max(len(pd["layers"]), len(rd["layers"]))
    return result["paired"] >= MIN_LAYERS and result["paired"] >= 0.6 * longest and len(result["diffs"]) <= MAX_DIFFS


def material_like(name: str) -> bool:
    """A layer name that carries a construction-material word; headers, colour codes and "см. альбом" remarks do not."""
    return bool(_MATERIAL_RE.search(name or ""))
