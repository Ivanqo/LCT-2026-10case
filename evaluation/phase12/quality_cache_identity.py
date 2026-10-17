"""Byte-identity of `_quality_for_page` results: no cache vs cold cache (written) vs warm cache (read back).

Takes the page list from the JSON written by quality_equivalence.py (so no re-planning over every PDF), keeps every
non-trivial page (heavy, stamps, rotated, images) and a stride of the rest, and compares the serialised results.
The OCR fallback is stubbed as "unavailable" (as in quality_equivalence.py); such results are, by design, never cached,
so they are recomputed on the warm pass and must still be identical.

  python evaluation/phase12/quality_cache_identity.py --root E:/case10_measurement/public_docs \
      --pages-json E:/case10_phase12/quality_perf/equivalence.json --cache-dir <empty dir> --out <report.json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "api_service"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--pages-json", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--stride", type=int, default=3)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from app.domain import dataset_sources, official_evidence

    def no_ocr(*_a, **_k):
        raise RuntimeError("OCR stubbed")

    dataset_sources._ocr_page_words = no_ocr
    rows = json.loads(Path(args.pages_json).read_text(encoding="utf-8"))["rows"]
    keep = [r for r in rows if r["mode"] != "full" or r["stamps"] or r["rotation"] or r["images"] >= 40 or r["annots"] >= 100]
    rest = [r for r in rows if r not in keep]
    pages = sorted(keep + rest[::args.stride], key=lambda r: (r["file"], r["page"]))
    root = Path(args.root)
    os.environ["CASE10_ORIGINALS_ROOT"] = str(root)
    cache_dir = Path(args.cache_dir)
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    shas: dict[str, str] = {}

    def run(label: str, with_cache: bool) -> tuple[list[bytes], float]:
        if with_cache:
            os.environ["CASE10_QUALITY_CACHE_DIR"] = str(cache_dir)
        else:
            os.environ.pop("CASE10_QUALITY_CACHE_DIR", None)
        out, started = [], time.perf_counter()
        for row in pages:
            if row["file"] not in shas:
                shas[row["file"]] = hashlib.sha256((root / row["file"]).read_bytes()).hexdigest()
            document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": row["file"]}},
                                       file_hash=shas[row["file"]], content_hash=shas[row["file"]], dataset_stage="PD")
            official_evidence._QUALITY_PAGE_CACHE.clear()  # in-memory cache off: only the disk cache may help
            out.append(json.dumps(official_evidence._quality_for_page(document, row["page"]), ensure_ascii=False).encode())
        return out, time.perf_counter() - started

    plain, plain_s = run("no_cache", False)
    cold, cold_s = run("cold", True)
    cached_files = len(list(cache_dir.rglob("*.json")))
    warm, warm_s = run("warm", True)
    result = {
        "pages": len(pages), "cache_files": cached_files,
        "identical_cold_vs_plain": sum(a == b for a, b in zip(plain, cold)),
        "identical_warm_vs_plain": sum(a == b for a, b in zip(plain, warm)),
        "seconds": {"no_cache": round(plain_s, 1), "cold_cache": round(cold_s, 1), "warm_cache": round(warm_s, 1)},
        "sha256_plain": hashlib.sha256(b"\n".join(plain)).hexdigest(),
        "sha256_warm": hashlib.sha256(b"\n".join(warm)).hexdigest(),
    }
    Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0 if (result["identical_cold_vs_plain"] == result["identical_warm_vs_plain"] == len(pages)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
