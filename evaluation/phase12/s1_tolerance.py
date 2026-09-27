"""S1: data behind the area tolerance -- |Δ| of every room matched by number between the paired PD and RD schedules
of an object, bucketed by relative change and by printed units (how many units of the coarser precision).

    python evaluation/phase12/s1_tolerance.py --object ALT79B --object DOO25 --object POL17 --object LOS3A
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "api_service"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.domain import explication_compare as ec  # noqa: E402
from app.domain import table_parser as tp  # noqa: E402
from evaluation.phase12 import s1_offline  # noqa: E402

REL_BUCKETS = (Decimal(0), Decimal("0.001"), Decimal("0.005"), Decimal("0.01"), Decimal("0.02"), Decimal("0.05"), Decimal("0.10"))


def bucket(value: Decimal) -> str:
    for low, high in zip(REL_BUCKETS, REL_BUCKETS[1:]):
        if value <= high:
            return f"<= {high * 100}%" if low == 0 else f"({low * 100}%; {high * 100}%]"
    return "> 10%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object", action="append", required=True)
    args = parser.parse_args()
    out = {}
    for obj in args.object:
        os.environ["CASE10_ORIGINALS_ROOT"] = str(s1_offline.NEW_OBJECTS_ROOT / s1_offline.FOLDERS[obj])
        docs, gate = s1_offline.object_docs(obj)
        candidates = ec.candidate_documents(docs, gate)
        refs, _ = ec.build_table_refs(candidates, gate)
        pd_refs, rd_refs = ec.merge_page_continuations(refs["PD"]), ec.merge_page_continuations(refs["RD"])
        rel, units, flagged = Counter(), Counter(), Counter()
        for pd, rd, *_ in ec.pair_tables(pd_refs, rd_refs, conflicting=ec._conflicts(gate, candidates)):
            pd_rows, _ = ec._keyed(pd.table)
            rd_rows, _ = ec._keyed(rd.table)
            for key in set(pd_rows) & set(rd_rows):
                left, right = tp.row_values(pd.table, pd_rows[key]), tp.row_values(rd.table, rd_rows[key])
                if tp.ROLE_AREA not in left or tp.ROLE_AREA not in right or not left[tp.ROLE_AREA]["value"]:
                    continue
                differs, delta = ec.numbers_differ(left[tp.ROLE_AREA], right[tp.ROLE_AREA], area=True)
                reference = left[tp.ROLE_AREA]["value"]
                decimals = min(left[tp.ROLE_AREA]["decimals"], right[tp.ROLE_AREA]["decimals"])
                rel[bucket(delta / abs(reference))] += 1
                units[str(min(int(delta / Decimal(1).scaleb(-decimals)), 5)) + ("+" if delta / Decimal(1).scaleb(-decimals) >= 5 else "")] += 1
                flagged[differs] += 1
        out[obj] = {"rooms_compared": sum(rel.values()), "relative": dict(sorted(rel.items())), "printed_units": dict(sorted(units.items())),
                    "flagged": {str(k): v for k, v in flagged.items()}}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
