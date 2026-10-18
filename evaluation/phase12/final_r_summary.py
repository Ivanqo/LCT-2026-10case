"""Step R: noise per object / per 100 pages and sampled precision (Wilson) from the hypothesis dumps of `final_b2_measure.py`.

    python evaluation/phase12/final_r_summary.py --hyp-dir E:/.../final_r/hyp [--labels labels.json]

`labels.json` maps "<OBJ>:<hypothesis id>" -> TRUE_DISCREPANCY | EXPLAINED_DIFFERENCE | FALSE (set by looking at the rendered source pages).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "api_service"):
    sys.path.insert(0, str(_p))

from evaluation.grader_sim import wilson  # noqa: E402

METHODS = ("CHANGE_SHEET", "LAYER_COMPOSITION")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hyp-dir", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    args = parser.parse_args()
    labels = json.loads(args.labels.read_text(encoding="utf-8")) if args.labels and args.labels.is_file() else {}
    rows, pages_total, counts = [], 0, Counter()
    for path in sorted(args.hyp_dir.glob("*.hyp.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        pages = int(data.get("pages_in_manifest") or 0)
        pages_total += pages
        by = Counter(h["method"] for h in data["hypotheses"])
        counts.update(by)
        det = (data.get("summary") or {}).get("detectors") or {}
        rows.append({"object": data["object"], "pages": pages, "seconds": data.get("seconds"),
                     **{m: by.get(m, 0) for m in METHODS},
                     **{f"{m}_per_100": round(100 * by.get(m, 0) / pages, 3) if pages else None for m in METHODS},
                     "register_entries": (det.get("CHANGE_SHEET") or {}).get("entries"), "candidate_entries": (det.get("CHANGE_SHEET") or {}).get("candidate_entries"),
                     "pd_compositions": (det.get("LAYER_COMPOSITION") or {}).get("pd_compositions"), "rd_compositions": (det.get("LAYER_COMPOSITION") or {}).get("rd_compositions")})
    precision = {}
    for method in METHODS:
        mine = {key: label for key, label in labels.items() if key.split(":")[0] + ":" in key and label}
        # labels carry the method through the dumps: key -> method lookup
        per = Counter()
        for path in sorted(args.hyp_dir.glob("*.hyp.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for h in data["hypotheses"]:
                if h["method"] != method:
                    continue
                label = labels.get(f"{data['object']}:{h['id']}")
                if label:
                    per[label] += 1
        n = sum(per.values())
        k = per.get("TRUE_DISCREPANCY", 0)
        precision[method] = {"labelled": n, "true": k, "labels": dict(per), "wilson": wilson(k, n) if n else None}
    out = {"objects": rows, "pages_total": pages_total, "hypotheses": dict(counts),
           "per_100_pages": {m: round(100 * counts.get(m, 0) / pages_total, 3) if pages_total else None for m in METHODS}, "precision": precision}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
