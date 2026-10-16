"""`score_baseline.py` without its side effect on the repository: the original also rewrites
evaluation/phase12/example_submission_gold11.json (a delivery file that was removed on purpose). Same arguments.

    python evaluation/phase12/final_s1_score.py --runs <dir with A_organizer/B_live/new> --seeds <l1_results.json> \
        --out-json X.json --out-md X.md --copy-runs ""
"""
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT, REPO_ROOT / "api_service"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evaluation.phase12 import score_baseline  # noqa: E402

score_baseline._write_example = lambda *args, **kwargs: None

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(score_baseline.main())
