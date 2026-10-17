"""Equivalence of the optimised `official_evidence._quality_for_page` against the pre-optimisation reference.

Read-only over the PDFs. The OCR fallback is replaced by a "tesseract unavailable" stub on BOTH sides, so the
comparison is about the changed part (stamp annotations, image-over-text); rotation/threshold logic is shared.

  python evaluation/phase12/quality_equivalence.py --root E:/case10_measurement/public_docs --pages 320 \
      --out E:/case10_phase12/quality_equivalence.json

Pages: (1) every "interesting" page (stamp annotations, rotation, many images/annotations, no text) first,
(2) filled up with a deterministic stride sample. Reference per page: 5 s deadline -> full comparison; a page that does
not finish is "heavy" (at most --max-heavy such pages): the first --max-full-heavy of them get the literal old algorithm
with a 120 s deadline; when it does not finish (and for the other heavy pages) the new decision is compared with the
reference that memoises get_image_info and de-duplicates rectangles (same predicate, same digest matching).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "api_service"))
sys.path.insert(0, str(ROOT / "api_service" / "tests"))


def page_features(pdf):
    import fitz
    stamp = int(fitz.PDF_ANNOT_STAMP)
    for index in range(len(pdf)):
        page = pdf[index]
        annots = page.annot_xrefs()
        yield {
            "page": index + 1,
            "annots": len(annots),
            "stamps": sum(1 for entry in annots if int(entry[1]) == stamp),
            "images": len(page.get_images(full=True)),
            "rotation": int(page.rotation) % 360,
            "has_text": bool(page.get_text("text").strip()),
        }


def interesting(f) -> bool:
    return bool(f["stamps"] or f["rotation"] or f["images"] >= 40 or f["annots"] >= 100 or not f["has_text"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", required=True)
    parser.add_argument("--pages", type=int, default=320)
    parser.add_argument("--light-s", type=float, default=5.0)
    parser.add_argument("--heavy-s", type=float, default=120.0)
    parser.add_argument("--max-heavy", type=int, default=10)
    parser.add_argument("--max-full-heavy", type=int, default=3,
                        help="how many heavy pages also get the literal old algorithm (up to --heavy-s each)")
    parser.add_argument("--max-seconds", type=float, default=1500.0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import fitz
    from app.domain import dataset_sources, official_evidence
    from quality_reference_impl import ReferenceTimeout, reference_quality

    def no_ocr(*_a, **_k):
        raise RuntimeError("OCR stubbed for equivalence run")

    dataset_sources._ocr_page_words = no_ocr

    started = time.monotonic()
    files = []
    for root in args.root:
        for path in sorted(Path(root).rglob("*.pdf")):
            files.append((Path(root), path))
    plan = []
    for root, path in files:
        with fitz.open(path) as pdf:
            for f in page_features(pdf):
                plan.append({**f, "root": str(root), "file": str(path.relative_to(root)).replace("\\", "/")})
    must = [p for p in plan if interesting(p)]
    rest = [p for p in plan if not interesting(p)]
    # Cap the "interesting" set so the plain pages are still represented; stride keeps it deterministic.
    cap = int(args.pages * 0.7)
    if len(must) > cap:
        stride = len(must) / cap
        must = [must[int(i * stride)] for i in range(cap)]
    need = max(0, args.pages - len(must))
    stride = max(1, len(rest) // max(1, need))
    selected = must + rest[::stride][:need]
    print(f"files={len(files)} pages_total={len(plan)} selected={len(selected)} interesting={len(must)}", flush=True)

    rows, heavy_done = [], 0
    selected.sort(key=lambda p: (p["file"], p["page"]))
    cache_hash: dict[str, str] = {}
    for item in selected:
        if time.monotonic() - started > args.max_seconds:
            print("global time budget reached", flush=True)
            break
        path = Path(item["root"]) / item["file"]
        sha = cache_hash.get(str(path)) or hashlib.sha256(path.read_bytes()).hexdigest()
        cache_hash[str(path)] = sha
        os.environ["CASE10_ORIGINALS_ROOT"] = item["root"]
        document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": item["file"]}},
                                   file_hash=sha, content_hash=sha, dataset_stage="PD")
        official_evidence._QUALITY_PAGE_CACHE.clear()
        t = time.perf_counter()
        new = official_evidence._quality_for_page(document, item["page"])
        new_s = time.perf_counter() - t
        row = {k: item[k] for k in ("file", "page", "annots", "stamps", "images", "rotation", "has_text")}
        row.update(new=new, new_s=round(new_s, 3))
        with fitz.open(path) as pdf:
            def run(dedupe, limit):
                t0 = time.perf_counter()
                try:
                    return reference_quality(pdf, item["page"], dedupe=dedupe, deadline_s=limit, ocr_words=no_ocr), \
                        time.perf_counter() - t0
                except ReferenceTimeout:
                    return None, time.perf_counter() - t0
            old, old_s = run(False, args.light_s)
            mode = "full"
            if old is None:
                if heavy_done >= args.max_heavy:
                    mode = "skipped_heavy"
                else:
                    heavy_done += 1
                    old = None
                    if heavy_done <= args.max_full_heavy:  # the literal old algorithm, up to --heavy-s
                        old, old_s = run(False, args.heavy_s)
                        mode = "full_heavy"
                    if old is None:  # old algorithm too slow: same predicate, memoised infos + de-duplicated rectangles
                        old, old_s = run(True, args.heavy_s)
                        mode = "dedupe_heavy" if old is not None else "unresolved_heavy"
        row.update(mode=mode, old_s=round(old_s, 3), old=old)
        row["equal"] = None if old is None else (old == new)
        rows.append(row)
        if old is not None and old != new:
            print("MISMATCH", row["file"], row["page"], old, new, flush=True)
    done = [r for r in rows if r["equal"] is not None]
    summary = {
        "pages_compared": len(done),
        "mismatches": sum(1 for r in done if not r["equal"]),
        "by_mode": {m: sum(1 for r in rows if r["mode"] == m) for m in
                    ("full", "full_heavy", "dedupe_heavy", "unresolved_heavy", "skipped_heavy")},
        "with_stamp_annots": sum(1 for r in done if r["stamps"]),
        "rotated": sum(1 for r in done if r["rotation"]),
        "images_ge_40": sum(1 for r in done if r["images"] >= 40),
        "status": {s: sum(1 for r in done if r["new"].get("quality_status") == s) for s in ("OK", "LOW_QUALITY")},
        "reasons": {},
        "new_total_s": round(sum(r["new_s"] for r in rows), 2),
        "old_total_s_compared": round(sum(r["old_s"] for r in done), 2),
        "elapsed_s": round(time.monotonic() - started, 1),
    }
    for r in done:
        for reason in r["new"].get("reasons") or []:
            summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
    Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if summary["mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
