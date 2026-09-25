"""Phase 10 / prompt B acceptance on the 6 SILVER new objects (19 comparable groups of pass 1).

Reads two directories of `run_silver_objects.py` snapshots -- `--before` (code before prompt B) and `--after` (code with
the decision layer), both run with the SAME tagger -- plus the pass-1 SILVER labels, and reports:

  * ACCEPTANCE: for the 6 checks the SILVER review labelled NEEDS_REVIEW (corpus cannot support a verdict) --
    how many still carry a committed verdict (must be 0) and which reason the pipeline gave instead;
  * the same 19-group table before/after (truth, outcome, exported submission label, reason);
  * the SILVER strict-basis metrics before/after (`evaluate_silver.score`), so the effect on precision and on the
    single true positive is visible, not just claimed;
  * every committed group and its exported label, so nothing changes silently.

Labels are SILVER (single annotator, second review pending): the numbers are dev diagnostics, never an official metric.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evaluation" / "silver_new_objects_pass1"))

from evaluate_silver import load_jsonl, mech_outcome, score  # noqa: E402
from evaluation.exporter import _violation_label_for_group  # noqa: E402

LABELS = REPO / "evaluation" / "silver_labels" / "new_objects_review_pass1.jsonl"
VALUE_REVIEW = REPO / "evaluation" / "silver_labels" / "new_objects_value_review_pass1.jsonl"


def load_snapshots(directory: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(directory.glob("snapshot_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        by_code: dict[str, list[dict]] = defaultdict(list)
        for group in data["groups"]:
            by_code[group["parameter_code"]].append(group)
        out[data["object_code"]] = {"groups": by_code, "meta": {k: v for k, v in data.items() if k != "groups"}}
    return out


def _describe(groups: list[dict]) -> dict:
    if not groups:
        return {"outcome": "NO_GROUP", "label": None, "reason": None}
    committed = [g for g in groups if g["comparability_status"] == "COMPARABLE"]
    group = committed[0] if committed else groups[0]
    delta = group.get("delta") or {}
    return {
        "outcome": mech_outcome(groups),
        "label": _violation_label_for_group({"finding_status": group["finding_status"], "delta": delta}),
        "finding_status": group["finding_status"],
        "model": group.get("model_version"),
        "reason": delta.get("reason"),
        "trigger": (delta.get("trigger") or {}).get("evaluation"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    before, after = load_snapshots(args.before), load_snapshots(args.after)
    labels = load_jsonl(LABELS)
    value_rows = load_jsonl(VALUE_REVIEW)

    table = []
    for row in labels:
        obj, code = row["object_code"], row["parameter_code"]
        b = _describe(before.get(obj, {}).get("groups", {}).get(code, []))
        a = _describe(after.get(obj, {}).get("groups", {}).get(code, []))
        table.append({"check": f"{obj}::{code}", "truth": row["verdict"], "in_original_19": bool(row.get("in_original_19")), "before": b, "after": a})

    unsupported = [t for t in table if t["truth"] == "NEEDS_REVIEW"]
    committed_before = [t for t in unsupported if t["before"]["outcome"] not in {"ABSTAIN", "NO_GROUP"}]
    committed_after = [t for t in unsupported if t["after"]["outcome"] not in {"ABSTAIN", "NO_GROUP"}]
    acceptance = {
        "unsupported_checks": len(unsupported),
        "committed_before": len(committed_before),
        "committed_after": len(committed_after),
        "after_detail": {t["check"]: {"label": t["after"]["label"], "reason": t["after"]["reason"], "status": t["after"].get("finding_status")} for t in unsupported},
        "PASS": len(committed_after) == 0 and all(t["after"]["label"] in {"COMPARISON_IMPOSSIBLE", "MISSING_DOCUMENT"} and t["after"]["reason"] for t in unsupported),
    }

    metrics = {}
    for name, snaps in (("before", before), ("after", after)):
        report = score(labels, snaps, value_rows)
        strict = report["strict_basis"]
        metrics[name] = {
            "committed_groups": report["committed_groups"]["total"],
            "committed_by_truth": report["committed_groups"]["by_truth"],
            "coverage_of_determinate": strict["coverage_of_determinate_checks"],
            "resolved_correct": strict["resolved_correct"],
            "precision_of_CANDIDATE": strict["precision_of_CANDIDATE"],
            "npv_of_NEGATIVE_VERIFIED": strict["npv_of_NEGATIVE_VERIFIED"],
            "false_positive_rate_on_no_violation": strict["false_positive_rate_on_no_violation_checks"],
            "recall_of_violations_abstain_counts_as_miss": strict["recall_of_violations_abstain_counts_as_miss"],
            "evaluate_case10": {k: report["evaluate_case10_strict"].get(k) for k in ("finding_precision", "finding_recall", "finding_f1", "counts")},
        }

    label_counts = {}
    for name, snaps in (("before", before), ("after", after)):
        counts: Counter = Counter()
        reasons: Counter = Counter()
        for obj, snap in snaps.items():
            for groups in snap["groups"].values():
                for g in groups:
                    counts[_violation_label_for_group({"finding_status": g["finding_status"], "delta": g.get("delta") or {}})] += 1
                    reason = (g.get("delta") or {}).get("reason")
                    if reason:
                        reasons[reason] += 1
        label_counts[name] = {"labels": dict(counts), "reasons": dict(reasons)}

    committed_now = []
    for obj, snap in after.items():
        for code, groups in sorted(snap["groups"].items()):
            for g in groups:
                if g["comparability_status"] == "COMPARABLE":
                    committed_now.append({"check": f"{obj}::{code}", "status": g["finding_status"], "model": g.get("model_version"),
                                          "values": (g.get("delta") or {}).get("values"), "reason": (g.get("delta") or {}).get("reason"),
                                          "trigger": ((g.get("delta") or {}).get("trigger") or {}).get("evaluation")})

    report = {"acceptance": acceptance, "table": table, "metrics": metrics, "label_counts": label_counts, "committed_after": committed_now}
    print(json.dumps({"acceptance": acceptance, "metrics": metrics, "label_counts": label_counts}, ensure_ascii=False, indent=1))
    print("\n19-group table (check | truth | before -> after | reason)")
    for t in table:
        if t["in_original_19"] or t["truth"] == "NEEDS_REVIEW":
            print(f"  {t['check']:22s} {t['truth']:18s} {t['before']['outcome']:18s} -> {t['after']['outcome']:18s} {t['after']['label']} {t['after']['reason'] or ''}")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(0 if acceptance["PASS"] else 1)


if __name__ == "__main__":
    main()
