"""Manual-audit helper for Metric 1: prints a seeded sample of holdout outcomes (all failure classes + some CORRECT ones) with the
truth line, the pipeline's cited value/context and the automatic class, so the taxonomy rules and the labels can be checked by
eye.  The verdicts are written by the reviewer into audit_verdicts.json (see report), never by this script.

  python -m evaluation.measurement_bench.audit_sample [--n 40] --out FILE
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict

from .common import CORPUS_DIR, OUT_DIR, read_jsonl
from .seed_l1 import SEED


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--out", default=str(OUT_DIR / "audit_sample.txt"))
    a = ap.parse_args()
    res = json.loads((OUT_DIR / "metric1_results.json").read_text(encoding="utf-8"))["results"]
    corpus = {r["triple_id"]: r for r in read_jsonl(CORPUS_DIR / "value_corpus_v1.jsonl")}
    hold = [r for r in res if r["split"] == "holdout"]
    by_status = defaultdict(list)
    for r in hold:
        by_status[r["status"]].append(r)
    rng = random.Random(SEED + 7)
    picked = []
    per = max(3, a.n // max(1, len(by_status)))
    for st, rows in sorted(by_status.items()):
        rows = sorted(rows, key=lambda r: r["triple_id"])
        rng.shuffle(rows)
        picked += rows[:per]
    out = []
    for r in picked[: a.n]:
        t = corpus[r["triple_id"]]
        out.append(f"### {r['triple_id']} [{r['family']}] -> {r['status']}/{r.get('sub')}  (label conf {r['confidence']})")
        out.append(f"   TRUTH {t['true_value_printed']!r} @ {t['file_id']} p{t['page']}: {t['line_text'][:110]}")
        p = r.get("pipeline")
        if p:
            out.append(f"   PIPE  {p['value']!r} @ {p['file_id']} p{p['page']}: ...{(p.get('context') or '')[:150]}...")
        else:
            out.append(f"   PIPE  none   marker={r.get('doc_marker')}")
        if r.get("isolated_on_true_page"):
            out.append(f"   ISO   {r['isolated_on_true_page']}")
    text = "\n".join(out)
    open(a.out, "w", encoding="utf-8").write(text)
    print(a.out, len(picked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
