"""GRAPH evaluation of the GRAPHIC_DIFF method on a corpus object (no DB): the detector runs on stand-in documents built from the
folder layout (stage by folder, section by file name -- the same `document_facts` the service uses).

  python evaluation/phase12/graph_hyp_eval.py run   --object NOV --out E:/case10_graph/hyp --kinds stage
  python evaluation/phase12/graph_hyp_eval.py crops --out E:/case10_graph/hyp --object NOV --n 12     (renders the hypotheses for a manual check)

Reports: documents, pages scanned, pairs matched / compared, hypotheses and hypotheses per 100 pages scanned.
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
sys.path[:0] = [str(ROOT / "api_service"), str(ROOT), str(ROOT / "evaluation" / "phase12")]

from graph_pairs_eval import MAX_DOC_PAGES, OBJECTS, object_documents  # noqa: E402


def build_documents(name: str):
    docs, paths = [], {}
    for index, item in enumerate(object_documents(Path(OBJECTS[name])), 1):
        data_path = Path(item["path"])
        sha = hashlib.sha256(data_path.read_bytes()).hexdigest()
        docs.append(SimpleNamespace(id=index, filename=data_path.name, dataset_file_id=f"{name}-{index}", dataset_stage=item["stage"], doc_stage={"PD": "project", "RD": "working"}[item["stage"]],
                                    dataset_section=item["tags"][0], discipline=item["tags"][0], document_code=data_path.stem, file_hash=sha, content_hash=sha, revision=None,
                                    approval_status="UNKNOWN", dataset_metadata={"document_manifest": {"relative_path": item["rel"], "stage": item["stage"], "sha256": sha}}))
        paths[index] = (data_path, sha)
    return docs, paths


def cmd_run(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ["CASE10_SHEET_COMPARE_ENABLED"] = "1"
    os.environ["CASE10_GRAPHIC_DIFF_KINDS"] = args.kinds
    os.environ.setdefault("CASE10_SHEET_COMPARE_CACHE_DIR", str(out / "cache"))
    from app.domain.comparison_gate import GateContext
    from app.domain.free_hypotheses import graphic_diff
    from app.domain.sheet_compare import service

    docs, paths = build_documents(args.object)
    service._bytes = lambda d: (paths[int(d.id)][0].read_bytes(), paths[int(d.id)][1])  # type: ignore[assignment]
    gate = GateContext(docs)
    import fitz

    pages = 0
    for d in docs:
        with fitz.open(paths[int(d.id)][0]) as pdf:
            pages += min(len(pdf), MAX_DOC_PAGES)
    t0 = time.time()
    candidates, stats = graphic_diff.detect(docs, gate, object_id=args.object)
    seconds = time.time() - t0
    rows = []
    for c in candidates:
        rows.append({"object": args.object, "confidence": c.confidence, "location": c.location, "description": c.description, "details": c.details,
                     "evidence": [{"file": str(paths[int(r.document.id)][0]), "page": r.page, "stage": r.stage, "bbox": r.bbox_pdf, "size": [r.page_width, r.page_height]} for r in c.evidence]})
    report = {"object": args.object, "documents": len(docs), "pages_in_documents": pages, "kinds": args.kinds, "hypotheses": len(rows), "seconds": round(seconds, 1),
              "per_100_pages": round(100 * len(rows) / max(1, pages), 3), "per_100_pages_scanned": round(100 * len(rows) / max(1, stats.get("pages_scanned") or 1), 3), "stats": stats}
    (out / f"hyp_{args.object}.json").write_text(json.dumps({"report": report, "hypotheses": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


def cmd_crops(args) -> None:
    import fitz
    from PIL import Image

    out = Path(args.out)
    data = json.loads((out / f"hyp_{args.object}.json").read_text(encoding="utf-8"))
    target = out / f"crops_{args.object}"
    target.mkdir(exist_ok=True)
    for index, row in enumerate(data["hypotheses"][:args.n], 1):
        tiles = []
        for ev in row["evidence"]:
            with fitz.open(ev["file"]) as pdf:
                page = pdf[ev["page"] - 1]
                x0, y0, x1, y1 = ev["bbox"]
                pad = 40
                clip = fitz.Rect(max(0, x0 - pad), max(0, y0 - pad), min(page.rect.width, x1 + pad), min(page.rect.height, y1 + pad))
                scale = min(4.0, 500 / max(clip.width, clip.height))
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
                tiles.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
        canvas = Image.new("RGB", (sum(t.width for t in tiles) + 10, max(t.height for t in tiles)), "white")
        x = 0
        for tile in tiles:
            canvas.paste(tile, (x, 0))
            x += tile.width + 10
        canvas.save(target / f"h_{index:02d}.png")
    print(target)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--object", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--kinds", default="stage")
    c = sub.add_parser("crops")
    c.add_argument("--object", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--n", type=int, default=12)
    args = parser.parse_args()
    {"run": cmd_run, "crops": cmd_crops}[args.cmd](args)


if __name__ == "__main__":
    main()
