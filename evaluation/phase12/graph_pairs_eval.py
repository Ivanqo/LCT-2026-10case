"""GRAPH evaluation: sheet pairing over the real corpus (no DB; same code as the service: sheet_compare.meta / matching / raster).

  python evaluation/phase12/graph_pairs_eval.py pairs  --out E:/case10_graph/run1 [--objects LOS3A ALT79B ...]
      scans PD and RD documents of the same discipline, matches sheets, writes pairs.jsonl and per-object counts;
  python evaluation/phase12/graph_pairs_eval.py sample --out E:/case10_graph/run1 --n 20 --seed 20261019
      draws a stratified sample of the matched pairs and renders them side by side (sample/pair_XX.png) for a manual check;
  python evaluation/phase12/graph_pairs_eval.py diff   --out ... --kind revision|stage --n 12
      raster comparison (time, similarity, regions) of the best pairs of one kind.

The documents are classified by folder / file name only (the same facts as `document_facts`), never by a labelled example.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "api_service"), str(ROOT)]

from app.domain import document_facts as df  # noqa: E402
from app.domain.sheet_compare import matching, meta as sheet_meta, raster  # noqa: E402

OBJECTS = {
    "LOS3A": r"E:\CASE10_new_objects_extracted\Лосевская, 3А",
    "ALT79B": r"E:\CASE10_new_objects_extracted\Алтуфьевское, 79Б",
    "IZU12": r"E:\CASE10_new_objects_extracted\Изумрудная, 12",
    "OKT103": r"E:\CASE10_new_objects_extracted\Октябрьская 103",
    "POL25": r"E:\CASE10_new_objects_extracted\Полярная ул. 25_ ДОО220к.9",
    "POL17": r"E:\CASE10_new_objects_extracted\Полярная, 17",
    "TYU": r"E:\case10_measurement\public_docs\TYU",
    "NOV": r"E:\case10_measurement\public_docs\NOV",
}
MAX_DOC_PAGES = 400
MAX_DOC_PAIRS = 60


def stage_of(rel: str) -> str | None:
    low = rel.lower().replace("\\", "/")
    if "стадия рд" in low or "/полные разделы/" in low:
        return "RD"
    if "стадия п/" in low or "стадия п\\" in low:
        return "PD"
    if "исполнительн" in low or "/ид/" in low or low.startswith("ид/") or "аоср" in low:
        return None
    if "рабоч" in low or "/рд/" in low or low.startswith("рд/"):
        return "RD"
    if "проектн" in low or "/пд/" in low or low.startswith("пд/"):
        return "PD"
    return None


def object_documents(root: Path) -> list[dict]:
    docs = []
    for path in sorted(root.rglob("*.pdf")):
        rel = str(path.relative_to(root))
        stage = stage_of(rel)
        if stage is None:
            continue
        facts = df.parse_document_name(path.name, rel)
        if not facts["tags"]:
            continue
        docs.append({"path": str(path), "rel": rel, "stage": stage, "tags": sorted(facts["tags"]), "section_key": facts["section_key"], "family": facts["family"],
                     "markers": [list(m) for m in facts["markers"]], "size": path.stat().st_size})
    return docs


def _marker_key(doc: dict) -> list:
    return [(str(k), int(v or 0)) for k, v in doc["markers"]]


def scan(doc: dict) -> dict[int, dict]:
    data = Path(doc["path"]).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    import fitz

    with fitz.open(stream=data, filetype="pdf") as pdf:
        total = len(pdf)
    return sheet_meta.document_meta(data, sha, pages=range(1, min(total, MAX_DOC_PAGES) + 1)), total


def cmd_pairs(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CASE10_SHEET_COMPARE_CACHE_DIR", str(out / "cache"))
    rows, summary = [], {}
    for name in args.objects:
        root = Path(OBJECTS[name])
        t0 = time.time()
        docs = object_documents(root)
        pd = [d for d in docs if d["stage"] == "PD"]
        rd = [d for d in docs if d["stage"] == "RD"]
        pairs = []
        for left in pd:
            for right in rd:
                common = set(left["tags"]) & set(right["tags"])
                if common:
                    pairs.append((left, right, sorted(common)[0], "stage"))
        revisions = []
        for i, a in enumerate(rd):
            for b in rd[i + 1:]:
                if a["section_key"] and a["section_key"] == b["section_key"] and a["family"] and a["family"] == b["family"] and a["markers"] != b["markers"]:
                    revisions.append((a, b, a["section_key"], "revision"))
        random.Random(args.doc_seed).shuffle(pairs)   # a fixed shuffle: the first documents of the sorted tree are not the only ones sampled
        pairs = pairs[:args.max_doc_pairs] + revisions[:args.max_doc_pairs]
        metas: dict[str, dict] = {}
        pages = 0
        count = 0
        for left, right, discipline, kind in pairs:
            for doc in (left, right):
                if doc["path"] not in metas:
                    try:
                        metas[doc["path"]], total = scan(doc)
                        pages += len(metas[doc["path"]])
                    except Exception as exc:  # noqa: BLE001
                        print("scan failed", doc["rel"], exc)
                        metas[doc["path"]] = {}
            L = [{"key": n, "meta": m} for n, m in metas[left["path"]].items() if sheet_meta.is_drawing(m)]
            R = [{"key": n, "meta": m} for n, m in metas[right["path"]].items() if sheet_meta.is_drawing(m)]
            if kind == "revision" and left["markers"] and right["markers"] and _marker_key(left) > _marker_key(right):
                left, right, L, R = right, left, R, L
            for match in matching.match_sheets(L, R, same_series=kind == "revision", min_confidence=args.min_confidence):
                lm, rm = metas[left["path"]][match["left"]], metas[right["path"]][match["right"]]
                rows.append({"object": name, "kind": kind, "discipline": discipline, "left_file": left["rel"], "left_page": match["left"], "right_file": right["rel"],
                             "right_page": match["right"], "confidence": match["confidence"], "ambiguous": match["ambiguous"], "components": match["components"],
                             "left_title": lm.get("title"), "right_title": rm.get("title"), "left_path": left["path"], "right_path": right["path"]})
                count += 1
        summary[name] = {"documents": len(docs), "pd": len(pd), "rd": len(rd), "doc_pairs": len(pairs), "pages_scanned": pages, "pairs": count, "seconds": round(time.time() - t0, 1)}
        print(name, summary[name], flush=True)
    (out / "pairs.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    (out / "pairs_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


def _side_by_side(row: dict, target: Path) -> None:
    import fitz
    from PIL import Image, ImageDraw

    images = []
    for path, page in ((row["left_path"], row["left_page"]), (row["right_path"], row["right_page"])):
        with fitz.open(path) as pdf:
            p = pdf[page - 1]
            scale = 800 / max(p.rect.width, p.rect.height)
            pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    h = max(i.height for i in images) + 40
    canvas = Image.new("RGB", (sum(i.width for i in images) + 10, h), "white")
    x = 0
    for image in images:
        canvas.paste(image, (x, 40))
        x += image.width + 10
    draw = ImageDraw.Draw(canvas)
    draw.text((4, 2), f"L p{row['left_page']} {row['left_file'][-60:]}", fill="black")
    draw.text((4, 16), f"conf {row['confidence']:.2f}  [{row['left_title'] or ''}] || [{row['right_title'] or ''}]"[:200], fill="red")
    canvas.save(target)


def cmd_sample(args) -> None:
    out = Path(args.out)
    rows = [json.loads(line) for line in (out / "pairs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r["kind"] == args.kind]
    rng = random.Random(args.seed)
    by_object: dict[str, list] = {}
    for r in rows:
        by_object.setdefault(r["object"], []).append(r)
    # stratified: equal quota per object with pairs, remainder drawn from the pool; fixed seed
    names = sorted(by_object)
    chosen: list[dict] = []
    quota = max(1, args.n // max(1, len(names)))
    for name in names:
        pool = by_object[name]
        rng.shuffle(pool)
        chosen += pool[:quota]
    rest = [r for r in rows if r not in chosen]
    rng.shuffle(rest)
    chosen += rest[:max(0, args.n - len(chosen))]
    chosen = chosen[:args.n]
    target = out / f"sample_{args.kind}"
    target.mkdir(exist_ok=True)
    manifest = []
    for index, row in enumerate(chosen, 1):
        _side_by_side(row, target / f"pair_{index:02d}.png")
        manifest.append({"n": index, **{k: v for k, v in row.items() if k not in ("left_path", "right_path")}})
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(chosen)} pairs from {len(rows)} -> {target}")


def cmd_diff(args) -> None:
    out = Path(args.out)
    os.environ.setdefault("CASE10_SHEET_COMPARE_CACHE_DIR", str(out / "cache"))
    rows = [json.loads(line) for line in (out / "pairs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r["kind"] == args.kind and not r["ambiguous"]]
    rows.sort(key=lambda r: (-r["confidence"], r["left_file"], r["left_page"]))
    results = []
    for row in rows[:args.n]:
        a, b = Path(row["left_path"]).read_bytes(), Path(row["right_path"]).read_bytes()
        t0 = time.time()
        r = raster.compare_images(a, row["left_page"], b, row["right_page"])
        dt = time.time() - t0
        results.append({"object": row["object"], "left_file": row["left_file"][-60:], "right_file": row["right_file"][-60:], "pages": [row["left_page"], row["right_page"]],
                        "confidence": row["confidence"], "seconds": round(dt, 2), "comparable": r["comparable"], "reason": r.get("reason"), "similarity": r.get("similarity"),
                        "layout": r.get("layout_match"), "regions": len(r["regions"]), "alignment": (r.get("alignment") or {}).get("method")})
        print(results[-1], flush=True)
    (out / f"diff_{args.kind}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pairs")
    p.add_argument("--out", required=True)
    p.add_argument("--objects", nargs="+", default=["LOS3A", "ALT79B", "IZU12", "OKT103", "TYU"])
    p.add_argument("--min-confidence", type=float, default=0.55)
    p.add_argument("--max-doc-pairs", type=int, default=MAX_DOC_PAIRS)
    p.add_argument("--doc-seed", type=int, default=20261019)
    s = sub.add_parser("sample")
    s.add_argument("--out", required=True)
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--seed", type=int, default=20261019)
    s.add_argument("--kind", default="stage")
    d = sub.add_parser("diff")
    d.add_argument("--out", required=True)
    d.add_argument("--kind", default="stage")
    d.add_argument("--n", type=int, default=12)
    args = parser.parse_args()
    {"pairs": cmd_pairs, "sample": cmd_sample, "diff": cmd_diff}[args.cmd](args)


if __name__ == "__main__":
    main()
