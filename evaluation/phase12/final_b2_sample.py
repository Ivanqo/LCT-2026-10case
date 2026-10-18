"""Step B2: render the cited places of sampled hypotheses (PD left, RD right) for manual labelling.

    python evaluation/phase12/final_b2_sample.py --db <nomx db> --obj ALT79B --hyp <obj.hyp.json> --out <dir> [--per-method 10] [--ids 1,2]

The sample is deterministic: per method, the hypotheses are ordered by id and every k-th is taken. One PNG per hypothesis
(<out>/<obj>_<id>.png) and an index (<out>/<obj>_index.json) with the description, the values and the references.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_baseline as rb  # noqa: E402


def pick(rows, per_method):
    by_method = {}
    for row in rows:
        by_method.setdefault(row["method"], []).append(row)
    out = []
    for method, items in sorted(by_method.items()):
        items.sort(key=lambda r: r["id"])
        step = max(1, len(items) // per_method) if len(items) > per_method else 1
        out.extend(items[::step][:per_method])
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--obj", required=True)
    parser.add_argument("--hyp", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--per-method", type=int, default=10)
    parser.add_argument("--ids", default="")
    args = parser.parse_args()
    sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "api_service")]
    os.environ.pop("CASE10_ORIGINALS_ROOT", None)
    if args.obj in rb.NEW:
        os.environ["CASE10_ORIGINALS_ROOT"] = str(rb.NEW_OBJECTS_ROOT / rb.NEW[args.obj])
    elif (rb.BENCH_WORK / "public_docs" / args.obj).is_dir():
        os.environ["CASE10_ORIGINALS_ROOT"] = str(rb.BENCH_WORK / "public_docs" / args.obj)
    import fitz
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import DocumentVersion
    from app.domain import dataset_sources

    data = json.loads(args.hyp.read_text(encoding="utf-8"))
    rows = data["hypotheses"]
    if args.ids:
        wanted = {int(x) for x in args.ids.split(",")}
        chosen = [r for r in rows if r["id"] in wanted]
    else:
        chosen = pick(rows, args.per_method)
    db = sessionmaker(bind=create_engine(f"sqlite+pysqlite:///{args.db.as_posix()}"))()
    args.out.mkdir(parents=True, exist_ok=True)
    index = []
    for row in chosen:
        crops = []
        for ev in [e for e in row["evidence"] if e["role"] in ("expected", "actual")][:2]:
            doc = db.get(DocumentVersion, ev["doc_id"])
            try:
                pdf = fitz.open(stream=dataset_sources.original_document_bytes(doc), filetype="pdf")
                page = pdf[int(ev["page"]) - 1]
                x0, y0, x1, y1 = ev["bbox_pdf"]
                clip = fitz.Rect(max(0, x0 - 30), max(0, y0 - 60), min(page.rect.width, x1 + 30), min(page.rect.height, y1 + 60))
                if clip.height > 500:
                    clip.y1 = clip.y0 + 500
                pix = page.get_pixmap(clip=clip, dpi=130)
                # draw the cited box
                crops.append((ev, pix))
            except Exception as exc:  # noqa: BLE001
                crops.append((ev, None))
                print("render failed", row["id"], exc)
        pixes = [p for _, p in crops if p is not None]
        if pixes:
            height = max(p.height for p in pixes)
            width = sum(p.width for p in pixes) + 10 * (len(pixes) - 1)
            sheet = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
            sheet.clear_with(255)
            x = 0
            for p in pixes:
                p = fitz.Pixmap(fitz.csRGB, p) if p.colorspace != fitz.csRGB else p
                p.set_origin(x, 0)
                sheet.copy(p, p.irect)
                x += p.width + 10
            sheet.save(str(args.out / f"{args.obj}_{row['id']}.png"))
        index.append({"id": row["id"], "method": row["method"], "detector": row["detector"], "description": row["description"],
                      "expected": row["expected"], "actual": row["actual"], "location": row["location"],
                      "evidence": [{k: e[k] for k in ("file", "stage", "page", "bbox_pdf", "role")} for e in row["evidence"]]})
    (args.out / f"{args.obj}_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(index), "rendered to", args.out)


if __name__ == "__main__":
    main()
