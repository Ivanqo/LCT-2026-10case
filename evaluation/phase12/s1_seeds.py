"""S1 L1 seeds: mutate an RD schedule's text layer (area / name / row) and check the mechanism end to end
(page words -> table_parser -> explication_compare).

World = one real schedule table T of an object. PD := T as parsed from the untouched page, RD := the same page with
one mutation applied to its WORDS (so the parser has to re-read the mutated sheet). Must-catch mutations:
AREA_UP (+5 %), AREA_DOWN (-15 %), NAME_SWAP (another function), ROW_DROP (a room disappears).
Must-not-catch: AREA_ROUNDING (one unit of the printed precision, <= 1 %), DECIMAL_FORMAT (point <-> comma),
NAME_TYPO (two adjacent letters swapped). Reported: recall on must-catch (right type at the seeded row), FP on
untouched rows (any violation on another row of a seeded world), FP of must-not-catch worlds.

    python evaluation/phase12/s1_seeds.py --object ALT79B --object LOS3A --out dev.json    (dev objects)
    python evaluation/phase12/s1_seeds.py --object DOO25 --object POL17 --out holdout.json (holdout objects)
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "api_service"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.domain import explication_compare as ec  # noqa: E402
from app.domain import table_parser as tp  # noqa: E402
from evaluation.grader_sim import wilson  # noqa: E402
from evaluation.phase12 import s1_offline  # noqa: E402

MUST_CATCH = ("AREA_UP", "AREA_DOWN", "NAME_SWAP", "ROW_DROP")
MUST_NOT = ("AREA_ROUNDING", "DECIMAL_FORMAT", "NAME_TYPO")
EXPECTED_TYPE = {"AREA_UP": ec.TYPE_AREA_CHANGED, "AREA_DOWN": ec.TYPE_AREA_CHANGED, "NAME_SWAP": ec.TYPE_NAME_CHANGED, "ROW_DROP": ec.TYPE_ONLY_PD}
REPLACEMENT_NAMES = ("Кладовая уборочного инвентаря", "Электрощитовая", "Комната отдыха персонала", "Серверная")


def page_snapshot(path: Path, page: int) -> dict[str, Any]:
    import fitz

    from app.domain.anchor_search import cluster_rows
    from app.domain.dataset_sources import _bbox_to_visible_frame, _visible_page_dims

    with fitz.open(path) as pdf:
        p = pdf[page - 1]
        rotation = int(p.rotation)
        rw, rh = float(p.cropbox.width), float(p.cropbox.height)
        width, height = _visible_page_dims(rw, rh, rotation)
        words = [{"bbox": _bbox_to_visible_frame([float(v) for v in w[:4]], rw, rh, rotation), "text": w[4]}
                 for w in p.get_text("words", sort=True) if str(w[4]).strip()]
        if rotation % 360:
            words = [w for row in cluster_rows(words) for w in row]
    return {"page": page, "width": width, "height": height, "words": words}


def _inside(word: dict, box: list[float]) -> bool:
    cx = (word["bbox"][0] + word["bbox"][2]) / 2
    cy = (word["bbox"][1] + word["bbox"][3]) / 2
    return box[0] - 0.5 <= cx <= box[2] + 0.5 and box[1] - 0.5 <= cy <= box[3] + 0.5


def _role_of(table: tp.RoomTable, word: dict) -> str:
    cx = (word["bbox"][0] + word["bbox"][2]) / 2
    return table.columns[tp._column_for(cx, table.columns)].role


def _fmt_like(value: Decimal, raw: str) -> str:
    decimals = tp.decimals_of(raw)
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",") if "," in raw else text


def mutate(snapshot: dict, table: tp.RoomTable, row: tp.Row, kind: str, rng: random.Random) -> dict | None:
    words = [dict(w) for w in snapshot["words"]]
    members = [w for w in words if _inside(w, row.bbox)]
    area_words = [w for w in members if _role_of(table, w) in tp.AREA_ROLES and tp.parse_decimal(w["text"]) is not None]
    name_words = [w for w in members if _role_of(table, w) in tp.TEXT_ROLES]
    if kind in ("AREA_UP", "AREA_DOWN", "AREA_ROUNDING", "DECIMAL_FORMAT"):
        if not area_words:
            return None
        target = area_words[0]
        value = tp.parse_decimal(target["text"])
        decimals = tp.decimals_of(target["text"])
        if kind == "AREA_UP":
            new = _fmt_like(value * Decimal("1.05") + Decimal(1).scaleb(-decimals) * 3, target["text"])
        elif kind == "AREA_DOWN":
            new = _fmt_like(value * Decimal("0.85"), target["text"])
        elif kind == "AREA_ROUNDING":
            new = _fmt_like(value + Decimal(1).scaleb(-decimals), target["text"])      # one printed unit: rounding noise
        else:
            if decimals == 0:
                return None
            new = target["text"].replace(",", "#").replace(".", ",").replace("#", ".")
        if new == target["text"]:
            return None
        target["text"] = new
    elif kind in ("NAME_SWAP", "NAME_TYPO"):
        if not name_words:
            return None
        current = " ".join(w["text"] for w in name_words)
        if kind == "NAME_SWAP":
            choices = [n for n in REPLACEMENT_NAMES if not ec.names_equal(n, current)]
            replacement = rng.choice(choices)
        else:
            longest = max(name_words, key=lambda w: len(w["text"]))
            text = longest["text"]
            if len(text) < 8:
                return None
            i = len(text) // 2
            longest["text"] = text[:i - 1] + text[i] + text[i - 1] + text[i + 1:]
            return {**snapshot, "words": words}
        first = name_words[0]
        ids = {id(w) for w in name_words[1:]}
        words = [w for w in words if id(w) not in ids]
        first["text"] = replacement
    elif kind == "ROW_DROP":
        ids = {id(w) for w in members}
        words = [w for w in words if id(w) not in ids]
    return {**snapshot, "words": words}


def worlds_for_object(obj: str, rng_seed: str) -> list[dict[str, Any]]:
    os.environ["CASE10_ORIGINALS_ROOT"] = str(s1_offline.NEW_OBJECTS_ROOT / s1_offline.FOLDERS[obj])
    docs, gate = s1_offline.object_docs(obj)
    candidates = ec.candidate_documents(docs, gate)
    refs, _ = ec.build_table_refs(candidates, gate)
    root = s1_offline.NEW_OBJECTS_ROOT / s1_offline.FOLDERS[obj]
    by_file = {d.dataset_file_id: d for d in docs}
    out = []
    seen_content: set[str] = set()
    for ref in sorted(refs["RD"], key=lambda r: (r.file_id, r.page, r.table.bbox[0])):
        table = ref.table
        if len(table.rows) < 3 or table.kind != tp.KIND_ROOMS:
            continue
        signature = ec.content_signature(table)
        if signature in seen_content:
            continue
        seen_content.add(signature)
        path = root / by_file[ref.file_id].dataset_metadata["document_manifest"]["relative_path"].replace("\\", "/")
        snapshot = page_snapshot(path, ref.page)
        original = [t for t in tp.find_room_tables(snapshot, page=ref.page) if ec.content_signature(t) == signature]
        if not original:
            continue
        base = original[0]
        rng = random.Random(hashlib.sha256(f"{rng_seed}|{ref.file_id}|{ref.page}|{signature}".encode()).hexdigest())
        for kind in MUST_CATCH + MUST_NOT:
            row = rng.choice(base.rows)
            mutated = mutate(snapshot, base, row, kind, rng)
            if mutated is None:
                continue
            out.append({"object": obj, "file_id": ref.file_id, "page": ref.page, "table": base.title[:60], "rows": len(base.rows),
                        "kind": kind, "row": row.key, "snapshot": mutated, "base": base, "bbox": list(table.bbox)})
    return out


def evaluate(world: dict[str, Any]) -> dict[str, Any]:
    base: tp.RoomTable = world["base"]
    tables = tp.find_room_tables(world["snapshot"], page=world["page"])
    pd = ec.TableRef(table=base, stage="PD", file_id="SEED-PD", doc_key="pd")
    rd_refs = [ec.TableRef(table=t, stage="RD", file_id="SEED-RD", doc_key="rd") for t in tables]
    results = ec.compare_pairs([pd], rd_refs)
    flagged = [(d.type, tp.normalize_key(d.location)) for r in results for d in r.discrepancies if d.violation and d.type != ec.TYPE_TOTAL]
    seeded_key = tp.normalize_key(world["row"])
    hit = (EXPECTED_TYPE.get(world["kind"]), seeded_key) in flagged
    other = {key for _, key in flagged if key != seeded_key}
    return {"paired": bool(results), "hit": hit, "flagged": flagged, "untouched_flagged": len(other), "untouched": max(0, world["rows"] - 1)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object", action="append", required=True)
    parser.add_argument("--seed", default="s1-l1-v1")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    rows = []
    for obj in args.object:
        for world in worlds_for_object(obj, args.seed):
            result = evaluate(world)
            rows.append({k: v for k, v in world.items() if k not in ("snapshot", "base")} | result)
    catch = [r for r in rows if r["kind"] in MUST_CATCH]
    must_not = [r for r in rows if r["kind"] in MUST_NOT]
    report = {
        "objects": args.object, "worlds": len(rows), "by_kind": dict(Counter(r["kind"] for r in rows)),
        "recall_must_catch": wilson(sum(r["hit"] for r in catch), len(catch)),
        "recall_by_kind": {k: wilson(sum(r["hit"] for r in catch if r["kind"] == k), sum(r["kind"] == k for r in catch)) for k in MUST_CATCH},
        "fp_untouched_rows": wilson(sum(r["untouched_flagged"] for r in rows), sum(r["untouched"] for r in rows)),
        "fp_must_not_catch_worlds": wilson(sum(bool(r["flagged"]) for r in must_not), len(must_not)),
        "fp_by_kind": {k: wilson(sum(bool(r["flagged"]) for r in must_not if r["kind"] == k), sum(r["kind"] == k for r in must_not)) for k in MUST_NOT},
        "unpaired_worlds": sum(not r["paired"] for r in rows),
        "misses": [r for r in catch if not r["hit"]][:40],
        "must_not_flagged": [r for r in must_not if r["flagged"]][:40],
    }
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("objects", "worlds", "recall_must_catch", "recall_by_kind", "fp_untouched_rows", "fp_must_not_catch_worlds", "fp_by_kind", "unpaired_worlds")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
