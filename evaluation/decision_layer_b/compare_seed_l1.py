"""Phase 10 / prompt B, item 5: stand-A L1 seeds, before vs after the decision layer.

Input: two `l1_results.json` produced by `python -m evaluation.measurement_bench.seed_l1` in two ISOLATED work dirs
(`CASE10_BENCH_WORK=<dir>`, so stand A's own outputs are never overwritten) with `CASE10_BENCH_APP_ROOT` pointing at the
code tree before / after prompt B. The protocol level (`create_official_evidence_groups` of each tree) is compared.

Reported, case by case and in aggregate:
  * stand A's own scoring (INC_ABOVE is always "must catch");
  * trigger-aware scoring: a numeric change must be caught only when the catalog trigger of that code fires on it
    (`trigger_policy`, derived from the catalog wording only) -- the scoring prompt B's item 5 asks for ("FP на
    увеличение / в пределах допуска -> 0");
  * every must-catch case the after-tree does NOT flag, split into
      - GATED   : the decision layer abstained with a named reason (the pair is not a legitimate comparison: edition
                  conflict, section not named by the catalog, not a measurement, ...) -- a deliberate abstention;
      - MISSED  : the pair stayed comparable and the change was still not flagged -- a real recall loss.
Usage:  python compare_seed_l1.py <before/l1_results.json> <after/l1_results.json> [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "api_service"))

from app.domain import trigger_policy as tp  # noqa: E402

ABSTAIN_LABELS = {"COMPARISON_IMPOSSIBLE", "MISSING_DOCUMENT"}


def _num(text) -> Decimal | None:
    try:
        return Decimal(str(text).replace(" ", "").replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError):
        return None


def trigger_expectation(row: dict) -> bool:
    """Should this seeded change be flagged under the catalog trigger of its code?"""
    if not row["must_catch"]:
        return False
    rule = tp.TRIGGER_POLICY.get(row["code"])
    if rule is None or row["mutation"] not in {"DEC_ABOVE", "INC_ABOVE"}:
        return True                                    # DOWNGRADE / COMPONENT_DEC / ROW_DELETE: fire by construction
    if rule.kind == tp.DECREASE:
        return row["mutation"] == "DEC_ABOVE"
    if rule.kind == tp.INCREASE:
        return row["mutation"] == "INC_ABOVE"
    return True                                        # ANY_CHANGE(_PCT): stand A mutates beyond the stated percentage


def _proto(row: dict) -> dict:
    return row.get("protocol") or {}


def _flagged(row: dict) -> bool:
    return _proto(row).get("label") == "VIOLATION_PRESENT"


def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None}


def summarize(rows: list[dict], expectation) -> dict:
    live = [r for r in rows if r.get("label") != "NOT_APPLICABLE" and _proto(r).get("available")]
    must = [r for r in live if expectation(r)]
    mustnot = [r for r in live if not expectation(r)]
    caught = [r for r in must if _flagged(r)]
    caught_ev = [r for r in caught if _proto(r).get("evidence_ok")]
    fp = [r for r in mustnot if _flagged(r)]
    return {
        "must_catch": len(must), "recall": _rate(len(caught), len(must)), "recall_with_evidence": _rate(len(caught_ev), len(must)),
        "must_not_catch": len(mustnot), "fp": _rate(len(fp), len(mustnot)),
        "fp_cases": sorted(r["case_id"] for r in fp),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    before = {r["case_id"]: r for r in json.loads(args.before.read_text(encoding="utf-8"))["rows"]}
    after = {r["case_id"]: r for r in json.loads(args.after.read_text(encoding="utf-8"))["rows"]}
    ids = sorted(set(before) & set(after))
    b_rows, a_rows = [before[i] for i in ids], [after[i] for i in ids]
    stand_a = lambda r: bool(r["must_catch"])  # noqa: E731

    losses = []
    for i in ids:
        b, a = before[i], after[i]
        if not trigger_expectation(a) or not _flagged(b) or _flagged(a):
            continue
        kind = "GATED" if _proto(a).get("label") in ABSTAIN_LABELS and _proto(a).get("reason") else "MISSED"
        losses.append({"case_id": i, "kind": kind, "after_label": _proto(a).get("label"), "reason": _proto(a).get("reason")})
    gained_fp_fix = [i for i in ids if not trigger_expectation(after[i]) and _flagged(before[i]) and not _flagged(after[i])]

    comparable = [i for i in ids if not (_proto(after[i]).get("label") in ABSTAIN_LABELS and _proto(after[i]).get("reason"))]
    report = {
        "cases_compared": len(ids),
        "stand_A_scoring": {"before": summarize(b_rows, stand_a), "after": summarize(a_rows, stand_a)},
        "trigger_aware_scoring": {"before": summarize(b_rows, trigger_expectation), "after": summarize(a_rows, trigger_expectation)},
        "trigger_aware_on_pairs_the_gate_keeps": {
            "cases": len(comparable),
            "before": summarize([before[i] for i in comparable], trigger_expectation),
            "after": summarize([after[i] for i in comparable], trigger_expectation),
        },
        "recall_losses": losses,
        "recall_losses_by_kind": dict(Counter(x["kind"] for x in losses)),
        "gated_reasons": dict(Counter(x["reason"] for x in losses if x["kind"] == "GATED")),
        "false_positives_removed": gained_fp_fix,
        "after_labels": dict(Counter(_proto(after[i]).get("label") for i in ids)),
    }
    print(json.dumps({k: v for k, v in report.items() if k != "recall_losses"}, ensure_ascii=False, indent=1))
    for x in losses:
        print("  loss:", json.dumps(x, ensure_ascii=False))
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
