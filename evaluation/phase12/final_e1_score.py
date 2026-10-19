"""Step E1: score the Docker CLI results (`python -m app.cli run`, blind, offline) with grader_sim on sets A/B, TYU and C.

Input: a directory with `<OBJ>/run1/result.json` per object (written by `deploy/tools/docker_timing.py`). The CLI result is the
1.1 submission, so it is scored exactly like the `B_live` baseline of `score_baseline.py`. Label sets need the organizer files
on disk (`case_data/`, not shipped); the output has metrics only.

    python evaluation/phase12/final_e1_score.py E:/case10_e1/cli --out E:/case10_e1/e1_metrics.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT, REPO_ROOT / "api_service"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evaluation import grader_sim as gs  # noqa: E402
from evaluation.phase12 import gold_sets  # noqa: E402
from evaluation.phase12.score_baseline import NEW, OFFICIAL  # noqa: E402


def _load(root: Path, objects) -> list[dict]:
    out: list[dict] = []
    for obj in objects:
        for name in (f"{obj}/run1/result.json", f"{obj}.submission.json"):
            path = root / name
            if path.is_file():
                out.extend(gs.load_submission(path))
                break
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    registry = gold_sets.registry()
    live = _load(args.root, OFFICIAL)
    new = _load(args.root, NEW)
    gold_a = gold_sets.set_a()
    gold_c, c_stats = gold_sets.set_c()
    tyu = [g for g in gold_a if g["object_id"] == OFFICIAL["TYU"]]
    reports = {
        "B": gs.evaluate(gold_a, live + new, registry=registry),
        "B_objects_on_disk": gs.evaluate([g for g in gold_a if g["available"]], live + new, registry=registry),
        "TYU_live": gs.evaluate(tyu, live, registry=registry),
        "C": gs.evaluate(gold_c, live + new, registry=registry),
    }
    args.out.write_text(json.dumps({"reports": reports, "C_label_stats": c_stats}, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for name, report in reports.items():
        sample = report["sample"]
        print(gs.render_summary(report, f"{name} - gold {sample['gold']} (+{sample['gold_positive']}/-{sample['gold_negative']}), "
                                        f"predictions {sample['predictions']} (VIOLATION_PRESENT {sample['predictions_positive']})"))
        primary = report["variants"][gs.PRIMARY_VARIANT]
        print(f"found: {', '.join(primary['found']) or '-'}; missed: {', '.join(primary['missed']) or '-'}; "
              f"negatives flagged: {', '.join(primary['negatives_flagged']) or '-'}; outcomes: {primary['outcomes'] or '-'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
