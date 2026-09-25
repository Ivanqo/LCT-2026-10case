"""Independent PDF reader for corpus labelling.

Reads the ORIGINAL PDFs through `silver_new_objects_pass1/probe.py`'s
rotation-aware text cache (plain fitz words -> row clustering) -- it never
touches the pipeline, the DB or any extractor, so a label written with this
tool is independent of the mechanism under test.

  reader.py grep OBJ RX [RX ...] [--stage PD|RD|ID] [--files ID ...] [--ctx N] [--max N] --out FILE
  reader.py page FILE_ID PAGE [PAGE ...] --out FILE
  reader.py ls OBJ [RX] --out FILE
  reader.py cache OBJ [STAGE ...]      (build the text cache; public objects: extract PDFs first)

Windows console mangles Cyrillic -> everything goes to --out (UTF-8).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

from .common import MANIFESTS, OBJECTS, PUBLIC_DOCS_ROOT, SILVER_PASS1, TEXT_CACHE, read_jsonl

os.environ.setdefault("SILVER_TEXT_CACHE", str(TEXT_CACHE))
_spec = importlib.util.spec_from_file_location("silver_probe", SILVER_PASS1 / "probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)  # type: ignore[union-attr]


def manifest_rows(obj: str) -> list[dict]:
    info = OBJECTS[obj]
    if info["kind"] == "new":
        rows = read_jsonl(MANIFESTS / f"manifest_{obj}.jsonl")
        for r in rows:
            r["obj"] = obj
        return rows
    path = PUBLIC_DOCS_ROOT / f"manifest_{obj}.jsonl"
    rows = read_jsonl(path)
    for r in rows:
        r["obj"] = obj
    return rows


def pdf_of(row: dict) -> Path:
    if OBJECTS[row["obj"]]["kind"] == "new":
        return probe.pdf_path(row)
    return PUBLIC_DOCS_ROOT / row["obj"] / row["relative_path"].replace("/", os.sep)


def load_pages(file_id: str, obj: str | None = None) -> list[str]:
    cache = TEXT_CACHE / f"{file_id}.json.gz"
    if not cache.exists():
        raise FileNotFoundError(f"{file_id} not in text cache ({cache})")
    import gzip
    import json

    with gzip.open(cache, "rt", encoding="utf-8") as f:
        return json.load(f)


def _cache_worker(args: tuple[str, str]) -> tuple[str, int, int]:
    """Build the text cache entry for one PDF (module-level so a process pool can import it)."""
    import gzip
    import json

    file_id, path = args
    out = TEXT_CACHE / f"{file_id}.json.gz"
    if out.exists():
        return file_id, -1, 0
    import fitz

    try:
        doc = fitz.open(path)
    except Exception:  # noqa: BLE001
        return file_id, 0, 0
    pages, empty = [], 0
    for page in doc:
        try:
            t = probe._page_text(page)
        except Exception:  # noqa: BLE001
            t = ""
        pages.append(t)
        empty += not t.strip()
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False)
    return file_id, len(pages), empty


def cmd_cache(a) -> None:
    from concurrent.futures import ProcessPoolExecutor

    TEXT_CACHE.mkdir(parents=True, exist_ok=True)
    jobs = [
        (r["file_id"], str(pdf_of(r)))
        for r in manifest_rows(a.obj)
        if (not a.stage or r["stage"] in a.stage) and str(r["relative_path"]).lower().endswith(".pdf")
    ]
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for file_id, n, empty in ex.map(_cache_worker, jobs):
            print(file_id, n, empty, flush=True)


def _emit(text: str, out: str | None) -> None:
    target = Path(out) if out else TEXT_CACHE.parent / "out" / "reader_out.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"-> {target} ({len(text)} chars)")


def cmd_grep(a) -> None:
    rxs = [re.compile(x, re.I) for x in a.regex]
    rows = [r for r in manifest_rows(a.obj) if (not a.stage or r["stage"] == a.stage) and (not a.files or r["file_id"] in a.files)]
    chunks: list[str] = []
    total = 0
    for rx in a.regex:
        pass
    for rx_text, rx in zip(a.regex, rxs):
        hits = 0
        block: list[str] = []
        for r in rows:
            try:
                pages = load_pages(r["file_id"])
            except FileNotFoundError:
                continue
            for i, t in enumerate(pages, 1):
                lines = t.split("\n")
                for j, ln in enumerate(lines):
                    if rx.search(ln):
                        hits += 1
                        if hits <= a.max:
                            lo, hi = max(0, j - a.ctx), min(len(lines), j + a.ctx + 1)
                            block.append(
                                f"## {r['file_id']} [{r['stage']}] {r['relative_path'][-60:]} p.{i}\n" + "\n".join(lines[lo:hi]) + "\n"
                            )
        chunks.append(f"######## /{rx_text}/  hits={hits} (showing<={a.max}) files={len(rows)}\n" + "\n".join(block))
    _emit("\n".join(chunks), a.out)


def cmd_page(a) -> None:
    pages = load_pages(a.file_id)
    out = [f"## {a.file_id} ({len(pages)} pages)"]
    for p in a.pages:
        out.append(f"\n----- page {p} -----\n{pages[p - 1] if 1 <= p <= len(pages) else '(out of range)'}")
    _emit("\n".join(out), a.out)


def cmd_ls(a) -> None:
    rx = re.compile(a.regex, re.I) if a.regex else None
    lines = [
        f"{r['file_id']}\t{r['stage']}\t{r.get('section')}\t{r['pdf_pages']}\t{r['relative_path']}"
        for r in manifest_rows(a.obj)
        if not rx or rx.search(r["relative_path"])
    ]
    _emit("\n".join(lines), a.out)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grep")
    g.add_argument("obj")
    g.add_argument("regex", nargs="+")
    g.add_argument("--stage")
    g.add_argument("--files", nargs="*")
    g.add_argument("--ctx", type=int, default=1)
    g.add_argument("--max", type=int, default=25)
    g.add_argument("--out")
    g.set_defaults(fn=cmd_grep)
    p = sub.add_parser("page")
    p.add_argument("file_id")
    p.add_argument("pages", type=int, nargs="+")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_page)
    l = sub.add_parser("ls")
    l.add_argument("obj")
    l.add_argument("regex", nargs="?")
    l.add_argument("--out")
    l.set_defaults(fn=cmd_ls)
    c = sub.add_parser("cache")
    c.add_argument("obj")
    c.add_argument("--stage", nargs="*")
    c.add_argument("--workers", type=int, default=6)
    c.set_defaults(fn=cmd_cache)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
