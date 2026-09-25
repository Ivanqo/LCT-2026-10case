"""Manual-review helper: open the ORIGINAL PDFs of the 6 new SILVER objects
(E:\\CASE10_new_objects_extracted, hash-verified manifests in ./manifests) by
manifest file_id, without going through the pipeline/DB at all, so the
reviewer's reading is independent of the mechanism under test.

  probe.py cache  OBJ [STAGE ...] [--sections S ...]   build per-page text cache (rotation-aware reading order)
  probe.py grep   OBJ REGEX [--stage S] [--file ID ...] [--ctx N] [--max N] [--out FILE]
  probe.py page   FILE_ID PAGE [PAGE ...] [--out FILE]        dump page text (1-based)
  probe.py ls     OBJ [REGEX]                                 list manifest rows (file_id/stage/pages/path)
  probe.py render FILE_ID PAGE OUT.png [--zoom Z]             rasterise a page for visual checks

Console note: the Windows console mangles Cyrillic, so every command writes
UTF-8 to --out (default work/probe_out.txt) and only prints the path/count.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFESTS = HERE / "manifests"
CACHE = Path(os.environ.get("SILVER_TEXT_CACHE", str(HERE / "work" / "textcache")))
ROOT = Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"))
DIRNAMES = {
    "LOS3A": "Лосевская, 3А", "ALT79B": "Алтуфьевское, 79Б", "POL17": "Полярная, 17",
    "DOO25": "Полярная ул. 25_ ДОО220к.9", "OKT103": "Октябрьская 103", "IZM12": "Изумрудная, 12",
}


def manifest(obj: str) -> list[dict]:
    with open(MANIFESTS / f"manifest_{obj}.jsonl", encoding="utf-8") as f:
        return [json.loads(l) for l in f]


def all_rows() -> dict[str, dict]:
    rows = {}
    for obj in DIRNAMES:
        for r in manifest(obj):
            r["obj"] = obj
            rows[r["file_id"]] = r
    return rows


def pdf_path(row: dict) -> Path:
    return ROOT / DIRNAMES[row["obj"]] / row["relative_path"].replace("/", "\\")


def _page_text(page) -> str:
    """Visible-frame reading order: words -> rotation matrix -> row clustering."""
    import fitz
    words = page.get_text("words")
    if not words:
        return ""
    rot = page.rotation
    m = page.rotation_matrix
    items = []
    for x0, y0, x1, y1, w, *_ in words:
        if rot:
            p0 = fitz.Point(x0, y0) * m
            p1 = fitz.Point(x1, y1) * m
            x0, x1 = sorted((p0.x, p1.x))
            y0, y1 = sorted((p0.y, p1.y))
        items.append((x0, y0, x1, y1, w))
    items.sort(key=lambda t: ((t[1] + t[3]) / 2, t[0]))
    lines, cur, cur_y = [], [], None
    for it in items:
        cy = (it[1] + it[3]) / 2
        h = max(2.0, it[3] - it[1])
        if cur and abs(cy - cur_y) > 0.6 * h:
            lines.append(sorted(cur, key=lambda t: t[0]))
            cur = []
        if not cur:
            cur_y = cy
        cur.append(it)
    if cur:
        lines.append(sorted(cur, key=lambda t: t[0]))
    return "\n".join(" ".join(w[4] for w in ln) for ln in lines)


def _cache_one(args) -> tuple[str, int, int]:
    file_id, path = args
    out = CACHE / f"{file_id}.json.gz"
    if out.exists():
        return file_id, -1, 0
    import fitz
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        return file_id, 0, 0
    pages, empty = [], 0
    for page in doc:
        try:
            t = _page_text(page)
        except Exception:  # noqa: BLE001
            t = ""
        pages.append(t)
        empty += not t.strip()
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False)
    return file_id, len(pages), empty


def cmd_cache(a):
    CACHE.mkdir(parents=True, exist_ok=True)
    jobs = []
    for r in manifest(a.obj):
        if a.stage and r["stage"] not in a.stage:
            continue
        if a.sections and r["section"] not in a.sections:
            continue
        r["obj"] = a.obj
        jobs.append((r["file_id"], str(pdf_path(r))))
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for file_id, n, empty in ex.map(_cache_one, jobs):
            print(file_id, n, empty, flush=True)


def load_pages(file_id: str) -> list[str]:
    p = CACHE / f"{file_id}.json.gz"
    if not p.exists():
        _cache_one((file_id, str(pdf_path(all_rows()[file_id]))))
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def emit(a, text: str) -> None:
    out = Path(a.out) if getattr(a, "out", None) else HERE / "work" / "probe_out.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"-> {out} ({len(text)} chars)")


def cmd_grep(a):
    rx = re.compile(a.regex, re.I)
    rows = [r for r in manifest(a.obj) if (not a.stage or r["stage"] == a.stage) and (not a.file or r["file_id"] in a.file)]
    if a.cached:
        rows = [r for r in rows if (CACHE / f"{r['file_id']}.json.gz").exists()]
    out, hits = [], 0
    for r in rows:
        try:
            pages = load_pages(r["file_id"])
        except Exception as exc:  # noqa: BLE001
            out.append(f"## {r['file_id']} unreadable: {exc}")
            continue
        for i, t in enumerate(pages, 1):
            lines = t.split("\n")
            for j, ln in enumerate(lines):
                if rx.search(ln):
                    hits += 1
                    if hits <= a.max:
                        lo, hi = max(0, j - a.ctx), min(len(lines), j + a.ctx + 1)
                        out.append(f"## {r['file_id']} [{r['stage']}/{r['section']}] {r['relative_path']} p.{i} L{j}\n" + "\n".join(lines[lo:hi]) + "\n")
    out.insert(0, f"# {hits} hits for /{a.regex}/ in {len(rows)} files (showing <= {a.max})\n")
    emit(a, "\n".join(out))


def cmd_page(a):
    rows = all_rows()
    r = rows[a.file_id]
    pages = load_pages(a.file_id)
    out = [f"## {a.file_id} [{r['stage']}/{r['section']}] {r['relative_path']}  ({len(pages)} pages)"]
    for p in a.pages:
        out.append(f"\n----- page {p} -----\n{pages[p - 1] if 1 <= p <= len(pages) else '(out of range)'}")
    emit(a, "\n".join(out))


def cmd_ls(a):
    rx = re.compile(a.regex, re.I) if a.regex else None
    lines = [f"{r['file_id']}\t{r['stage']}\t{r['section']}\t{r['pdf_pages']}\t{r['relative_path']}" for r in manifest(a.obj) if not rx or rx.search(r["relative_path"])]
    emit(a, "\n".join(lines))


def cmd_render(a):
    import fitz
    rows = all_rows()
    doc = fitz.open(pdf_path(rows[a.file_id]))
    pix = doc[a.page - 1].get_pixmap(matrix=fitz.Matrix(a.zoom, a.zoom))
    pix.save(a.out_png)
    print("->", a.out_png, pix.width, pix.height)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cache"); c.add_argument("obj"); c.add_argument("stage", nargs="*"); c.add_argument("--workers", type=int, default=4); c.add_argument("--sections", nargs="*"); c.set_defaults(fn=cmd_cache)
    g = sub.add_parser("grep"); g.add_argument("obj"); g.add_argument("regex"); g.add_argument("--stage"); g.add_argument("--file", nargs="*"); g.add_argument("--ctx", type=int, default=1); g.add_argument("--max", type=int, default=60); g.add_argument("--out"); g.add_argument("--cached", action="store_true", help="only search files already in the text cache"); g.set_defaults(fn=cmd_grep)
    p = sub.add_parser("page"); p.add_argument("file_id"); p.add_argument("pages", type=int, nargs="+"); p.add_argument("--out"); p.set_defaults(fn=cmd_page)
    l = sub.add_parser("ls"); l.add_argument("obj"); l.add_argument("regex", nargs="?"); l.add_argument("--out"); l.set_defaults(fn=cmd_ls)
    r = sub.add_parser("render"); r.add_argument("file_id"); r.add_argument("page", type=int); r.add_argument("out_png"); r.add_argument("--zoom", type=float, default=1.5); r.set_defaults(fn=cmd_render)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
