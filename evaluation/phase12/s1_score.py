"""S1: grader_sim on set B (live mode) -- baseline runs vs runs with CASE10_EXPLICATION_COMPARE=1.

    python evaluation/phase12/s1_score.py --official E:/case10_phase12/runs/B_live --new-base E:/case10_phase12/runs/new \
        --new-s1 E:/case10_phase12/runs/s1_new [--official-s1 <dir>] --out evaluation/phase12/s1_metrics.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "api_service"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evaluation import grader_sim as gs  # noqa: E402
from evaluation.phase12 import gold_sets  # noqa: E402
from evaluation.phase12.run_baseline import NEW, OFFICIAL  # noqa: E402

DOMAIN_EXPLICATION = ("ALT79B-V01", "DOO25-V01", "POL16-V01", "SOSH25-V01")


def preds(run_dir: Path, objects) -> list[dict]:
    out = []
    for obj in objects:
        path = run_dir / f"{obj}.submission.json"
        if path.is_file():
            out.extend(gs.load_submission(path))
    return out


def summary(report: dict) -> dict:
    primary = report["variants"][gs.PRIMARY_VARIANT]
    strict = report["variants"][gs.EVIDENCE_STRICT]
    return {
        "recall": primary["recall"], "precision_closed": primary["precision_closed"], "precision_open": primary["precision_open"],
        "f1_closed": primary["f1_closed"], "fpr": primary["false_positive_rate"], "negatives_confirmed": primary["negatives_confirmed"],
        "found": primary["found"], "negatives_flagged": primary["negatives_flagged"], "outcomes": primary["outcomes"],
        "key_only_found": report["variants"][gs.KEY_ONLY]["found"],
        "strict_recall": strict["recall"], "strict_found": strict["found"],
        "predictions_positive": report["sample"]["predictions_positive"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--official", type=Path, required=True)
    parser.add_argument("--official-s1", type=Path)
    parser.add_argument("--new-base", type=Path, required=True)
    parser.add_argument("--new-s1", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    golds = gold_sets.set_a()
    registry = gold_sets.registry()
    base = preds(args.official, OFFICIAL) + preds(args.new_base, NEW)
    s1 = preds(args.official_s1 or args.official, OFFICIAL) + preds(args.new_s1, NEW)
    on_disk = [g for g in golds if g["available"]]
    domain = [g for g in golds if g["gold_id"] in DOMAIN_EXPLICATION or g["gold_id"] == "POL17-N01"]
    reports = {
        "B_base": summary(gs.evaluate(golds, base, registry=registry)),
        "B_s1": summary(gs.evaluate(golds, s1, registry=registry)),
        "B_on_disk_base": summary(gs.evaluate(on_disk, base, registry=registry)),
        "B_on_disk_s1": summary(gs.evaluate(on_disk, s1, registry=registry)),
        "explication_cases_base": summary(gs.evaluate(domain, base, registry=registry)),
        "explication_cases_s1": summary(gs.evaluate(domain, s1, registry=registry)),
    }
    text = json.dumps(reports, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
    sys.stdout.reconfigure(encoding="utf-8")
    for name, rep in reports.items():
        print(f"{name:24} recall {gs._fmt(rep['recall'])} P(closed) {gs._fmt(rep['precision_closed'])} P(open) {gs._fmt(rep['precision_open'])} "
              f"FPR {gs._fmt(rep['fpr'])} strict {gs._fmt(rep['strict_recall'])} found={rep['found']} strict_found={rep['strict_found']} "
              f"neg_flagged={rep['negatives_flagged']} pos_preds={rep['predictions_positive']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
