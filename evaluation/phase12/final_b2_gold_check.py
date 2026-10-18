"""Step B2 validation (no fitting): do the hypotheses cite the page pairs of the organizer's domain re-annotation?

    python evaluation/phase12/final_b2_gold_check.py --hyp-dir E:/.../hyp [--out result.json]

A positive case is "hit" when some hypothesis cites a PD page and an RD page of the annotated pair (same files, pages);
a negative case (a pair the organizer marked as no violation) is a false hit under the same rule. Page-pair level only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "api_service"):
    sys.path.insert(0, str(_p))

from evaluation.phase12 import gold_sets  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hyp-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    rows = []
    for case in gold_sets.domain_cases():
        if not case["available"]:
            continue
        obj = case["object_id"].replace("OBJ-NEW-", "")
        path = args.hyp_dir / f"{obj}.hyp.json"
        if not path.is_file():
            continue
        hyps = json.loads(path.read_text(encoding="utf-8"))["hypotheses"]
        gold_pages = {(e["file_id"], int(e["page"])): e["stage"] for e in case["evidence"]}
        hits = []
        for h in hyps:
            cited = {(e["file"], int(e["page"])) for e in h["evidence"]}
            stages = {gold_pages[k] for k in cited if k in gold_pages}
            if {"PD", "RD"} <= stages:
                hits.append({"id": h["id"], "method": h["method"], "location": h["location"]})
        rows.append({"gold_id": case["gold_id"], "label": case["label"], "score_eligible": case["score_eligible"], "source_status": case["source_status"],
                     "hypotheses_on_object": len(hyps), "hits": len(hits), "hit_examples": hits[:6]})
    out = {"cases": rows}
    text = json.dumps(out, ensure_ascii=False, indent=1)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
