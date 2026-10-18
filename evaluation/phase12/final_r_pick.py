"""Step R: deterministic sample of up to N hypotheses per detector across all objects, rendered for manual labelling.

    python evaluation/phase12/final_r_pick.py --hyp-dir E:/.../final_r/hyp --db-dir E:/.../final_b2/nomx --out E:/.../final_r/samples [--n 10]

The pool of a detector is every hypothesis of every object ordered by (object, id); n evenly spaced positions of the pool are taken, so
no hypothesis is chosen by its look. Calls `final_b2_sample.py` once per object with the chosen ids and prints the picks.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hyp-dir", type=Path, required=True)
    parser.add_argument("--db-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--methods", default="CHANGE_SHEET,LAYER_COMPOSITION")
    args = parser.parse_args()
    pool: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path in sorted(args.hyp_dir.glob("*.hyp.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for h in sorted(data["hypotheses"], key=lambda r: r["id"]):
            pool[h["method"]].append((data["object"], int(h["id"])))
    chosen: dict[str, list[int]] = defaultdict(list)
    picks = {}
    for method in args.methods.split(","):
        items = pool.get(method, [])
        picked = [items[int(i * len(items) / args.n)] for i in range(args.n)] if len(items) > args.n else list(items)
        picks[method] = {"pool": len(items), "picked": [f"{o}:{i}" for o, i in picked]}
        for obj, hid in picked:
            chosen[obj].append(hid)
    for obj, ids in sorted(chosen.items()):
        subprocess.run([sys.executable, str(HERE / "final_b2_sample.py"), "--db", str(args.db_dir / f"{obj}.db"), "--obj", obj,
                        "--hyp", str(args.hyp_dir / f"{obj}.hyp.json"), "--out", str(args.out), "--ids", ",".join(str(i) for i in ids)], check=False)
    print(json.dumps(picks, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
