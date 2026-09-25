"""SILVER evaluation of the live v3 pipeline on the 6 new objects (review pass 1).

Reads
    ../silver_labels/new_objects_review_pass1.jsonl        manual verdicts (blind + post-unblinding rows)
    ../silver_labels/new_objects_value_review_pass1.jsonl  per-committed-group review of the CITED values
    ./mechanism_snapshots/snapshot_<OBJ>.json              pipeline output (snapshot_pipeline.py)
and writes ../reports/new_objects_silver_pass1/silver_metrics.json (+ prints a compact table).

This is a SEPARATE, explicitly SILVER-tier metric (single annotator, same model family as the
system under test, second review PENDING, training_eligible=false). It must never be merged into
the official public-gold n=6 metric (smoke_real_data.py / evaluate_case10 on public objects); the
JSON carries `metric_tier` and `not_comparable_with` for that reason.

Ground-truth basis
    strict   : label.verdict (trigger-based, see labels_source.py docstring), determinate rows only
    lenient  : "cited PD and RD rows are genuine values of the parameter and differ" (needs value review)
`evaluate_case10()` is called on the strict basis: NEEDS_REVIEW/UNLABELED/EXCLUDED rows never enter the
gold; predictions are restricted to labeled checks so an un-reviewed CANDIDATE is never silently a FP.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from evaluation.metrics import _wilson_interval, evaluate_case10  # noqa: E402

LABELS = REPO / "evaluation" / "silver_labels" / "new_objects_review_pass1.jsonl"
VALUE_REVIEW = REPO / "evaluation" / "silver_labels" / "new_objects_value_review_pass1.jsonl"
SNAP_DIR = HERE / "mechanism_snapshots"
OUT_DIR = REPO / "evaluation" / "reports" / "new_objects_silver_pass1"
DETERMINATE = {"VIOLATION_PRESENT", "NO_VIOLATION"}


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def ci(k: int, n: int):
    if not n:
        return None
    lo, hi = _wilson_interval(k, n)
    return [round(lo, 3), round(hi, 3)]


def is_valid(judgement: str | None) -> bool:
    """VALID and VALID_NON_AUTHORITATIVE both mean 'the mechanism read a genuine statement of the parameter'."""
    return bool(judgement) and judgement.startswith("VALID")


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "value": round(k / n, 3) if n else None, "wilson95": ci(k, n)}


def load_snapshots() -> dict[str, dict[str, list[dict]]]:
    out: dict[str, dict[str, list[dict]]] = {}
    for p in sorted(SNAP_DIR.glob("snapshot_*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        by_code: dict[str, list[dict]] = defaultdict(list)
        for g in d["groups"]:
            by_code[g["parameter_code"]].append(g)
        out[d["object_code"]] = {"groups": by_code, "meta": {k: v for k, v in d.items() if k != "groups"}}
    return out


def mech_outcome(groups: list[dict]) -> str:
    comp = [g for g in groups if g["comparability_status"] == "COMPARABLE"]
    if not comp:
        return "ABSTAIN"
    return "CANDIDATE" if any(g["finding_status"] == "CANDIDATE" for g in comp) else "NEGATIVE_VERIFIED"


def score(labels: list[dict], snaps: dict[str, dict], value_rows: list[dict]) -> dict:
    """Pure scoring core (unit-tested): labels + snapshots + value review -> report dict."""
    by_id = {r["check_id"]: r for r in labels}
    value_by_id: dict[str, list[dict]] = defaultdict(list)
    for v in value_rows:
        value_by_id[v["check_id"]].append(v)

    # ---- every committed (COMPARABLE) group must carry a label, otherwise the precision denominator is silently biased
    committed: list[dict] = []
    unlabeled_committed: list[str] = []
    for obj, s in snaps.items():
        for code, groups in s["groups"].items():
            out = mech_outcome(groups)
            if out == "ABSTAIN":
                continue
            check_id = f"OBJ-NEW-{obj}::{code}"
            committed.append({"check_id": check_id, "object": obj, "code": code, "outcome": out})
            if check_id not in by_id:
                unlabeled_committed.append(check_id)

    rows = []
    for r in labels:
        obj, code = r["object_code"], r["parameter_code"]
        groups = snaps.get(obj, {}).get("groups", {}).get(code, [])
        rows.append({**r, "mech_outcome": mech_outcome(groups) if obj in snaps else "NO_SNAPSHOT"})

    det = [r for r in rows if r["verdict"] in DETERMINATE]

    # ---- evaluate_case10 on the strict basis
    gold_findings = [{"id": r["check_id"], "object_id": r["object_id"], "parameter_code": r["parameter_code"], "location": "",
                      "is_violation": r["verdict"] == "VIOLATION_PRESENT", "category": r["parameter_code"].split("-")[0]} for r in det]
    pred_findings = [{"id": r["check_id"], "object_id": r["object_id"], "parameter_code": r["parameter_code"], "location": "",
                      "status": {"ABSTAIN": "NOT_COMPARABLE"}.get(r["mech_outcome"], r["mech_outcome"]),
                      "category": r["parameter_code"].split("-")[0]} for r in det]
    e10 = evaluate_case10({"findings": gold_findings, "total_params": len(gold_findings)}, {"findings": pred_findings})
    keep = ["finding_precision", "finding_recall", "finding_f1", "false_positive_rate", "coverage", "abstention_rate", "counts",
            "finding_precision_sample_size", "finding_precision_confidence_interval_95", "finding_recall_sample_size",
            "finding_recall_confidence_interval_95", "finding_f1_confidence_interval_95", "false_positive_rate_confidence_interval_95",
            "per_category_metrics"]
    e10_out = {k: e10.get(k) for k in keep}

    # ---- crosstab
    ct = Counter((r["verdict"], r["mech_outcome"]) for r in det)
    tp, fp = ct[("VIOLATION_PRESENT", "CANDIDATE")], ct[("NO_VIOLATION", "CANDIDATE")]
    tn, fn = ct[("NO_VIOLATION", "NEGATIVE_VERIFIED")], ct[("VIOLATION_PRESENT", "NEGATIVE_VERIFIED")]
    ab_v, ab_n = ct[("VIOLATION_PRESENT", "ABSTAIN")], ct[("NO_VIOLATION", "ABSTAIN")]
    n_v, n_n = tp + fn + ab_v, fp + tn + ab_n
    committed_det = tp + fp + tn + fn
    strict = {
        "n_determinate": len(det), "n_violation_present": n_v, "n_no_violation": n_n,
        "crosstab": {f"{t}|{o}": c for (t, o), c in sorted(ct.items())},
        "coverage_of_determinate_checks": rate(committed_det, len(det)),
        "resolved_correct": rate(tp + tn, committed_det),
        "precision_of_CANDIDATE": rate(tp, tp + fp),
        "npv_of_NEGATIVE_VERIFIED": rate(tn, tn + fn),
        "recall_of_violations_abstain_counts_as_miss": rate(tp, n_v),
        "recall_of_violations_among_committed": rate(tp, tp + fn),
        "false_positive_rate_on_no_violation_checks": rate(fp, n_n),
        "note": "recall denominator = labeled VIOLATION_PRESENT checks only (n small); labeled set is NOT a random sample of the 132x6 universe",
    }

    # ---- label-uncertainty sensitivity: flip every MEDIUM-confidence determinate label (all 2^k combinations)
    from itertools import product
    medium = [r for r in det if r.get("verdict_confidence") == "MEDIUM"]
    scen = []
    for flips in product([False, True], repeat=len(medium)):
        flipped = {r["check_id"] for r, f in zip(medium, flips) if f}
        c = Counter()
        for r in det:
            truth = r["verdict"]
            if r["check_id"] in flipped:
                truth = "NO_VIOLATION" if truth == "VIOLATION_PRESENT" else "VIOLATION_PRESENT"
            c[(truth, r["mech_outcome"])] += 1
        s_tp, s_fp = c[("VIOLATION_PRESENT", "CANDIDATE")], c[("NO_VIOLATION", "CANDIDATE")]
        s_tn, s_fn = c[("NO_VIOLATION", "NEGATIVE_VERIFIED")], c[("VIOLATION_PRESENT", "NEGATIVE_VERIFIED")]
        scen.append({"flipped": sorted(flipped), "precision_of_CANDIDATE": round(s_tp / (s_tp + s_fp), 3) if s_tp + s_fp else None,
                     "npv_of_NEGATIVE_VERIFIED": round(s_tn / (s_tn + s_fn), 3) if s_tn + s_fn else None,
                     "resolved_correct": round((s_tp + s_tn) / (s_tp + s_fp + s_tn + s_fn), 3) if s_tp + s_fp + s_tn + s_fn else None,
                     "n_violation_present": s_tp + s_fn + c[("VIOLATION_PRESENT", "ABSTAIN")]})
    def _rng(key):
        vals = [x[key] for x in scen if x[key] is not None]
        return [min(vals), max(vals)] if vals else None
    sensitivity = {"medium_confidence_labels": [r["check_id"] for r in medium], "scenarios": len(scen),
                   "precision_of_CANDIDATE_range": _rng("precision_of_CANDIDATE"), "npv_of_NEGATIVE_VERIFIED_range": _rng("npv_of_NEGATIVE_VERIFIED"),
                   "resolved_correct_range": _rng("resolved_correct"), "n_violation_present_range": _rng("n_violation_present")}

    # ---- the original 19
    orig = [r for r in rows if r.get("in_original_19")]
    orig_summary = {
        "n": len(orig),
        "verdict_counts": dict(Counter(r["verdict"] for r in orig)),
        "outcome_now": dict(Counter(r["mech_outcome"] for r in orig)),
        "still_comparable_now": sum(r["mech_outcome"] != "ABSTAIN" for r in orig),
        "per_check": {r["check_id"].replace("OBJ-NEW-", ""): {"truth": r["verdict"], "mech": r["mech_outcome"]} for r in orig},
    }

    # ---- committed population incl. NEEDS_REVIEW (verdict semantics vs. label)
    committed_rows = []
    for c in committed:
        lab = by_id.get(c["check_id"])
        committed_rows.append({**c, "truth": lab["verdict"] if lab else "UNLABELED", "blind": bool(lab and lab.get("labelled_blind_to_current_mechanism_output"))})
    committed_by_truth = Counter(r["truth"] for r in committed_rows)

    # ---- value-level
    vsum: dict = {}
    if value_rows:
        per_group = {}
        for cid, vs in value_by_id.items():
            per_group[cid] = {v["stage"]: v["judgement"] for v in vs}
        both_valid = sum(1 for m in per_group.values() if is_valid(m.get("PD")) and is_valid(m.get("RD")))
        any_invalid = sum(1 for m in per_group.values() if any(not is_valid(j) for j in m.values()))
        stage_counts = Counter((v["stage"], v["judgement"]) for v in value_rows)
        vsum = {
            "groups_reviewed": len(per_group),
            "groups_with_both_stage_values_valid": rate(both_valid, len(per_group)),
            "groups_with_at_least_one_invalid_value": rate(any_invalid, len(per_group)),
            "stage_judgement_counts": {f"{s}|{j}": c for (s, j), c in sorted(stage_counts.items())},
            "committed_groups_without_value_review": sorted(c["check_id"] for c in committed if c["check_id"] not in per_group),
        }
        # verdict-correct-for-the-wrong-reason
        lucky = []
        strict_correct = []
        for r in det:
            m = per_group.get(r["check_id"])
            if not m:
                continue
            ok = (r["verdict"] == "VIOLATION_PRESENT" and r["mech_outcome"] == "CANDIDATE") or (r["verdict"] == "NO_VIOLATION" and r["mech_outcome"] == "NEGATIVE_VERIFIED")
            if ok:
                strict_correct.append(r["check_id"])
                if any(not is_valid(j) for j in m.values()):
                    lucky.append(r["check_id"])
        # validity by extractor family (model_version of the committed group)
        family: dict[str, str] = {}
        for obj, sn in snaps.items():
            for code, groups in sn["groups"].items():
                for g in groups:
                    if g["comparability_status"] == "COMPARABLE":
                        family[f"OBJ-NEW-{obj}::{code}"] = g.get("model_version") or "unknown"
        by_fam: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for cid, m in per_group.items():
            fam = family.get(cid, "unknown")
            by_fam[fam][1] += 1
            by_fam[fam][0] += int(all(is_valid(j) for j in m.values()))
        vsum["groups_with_both_values_valid_by_extractor"] = {fam: rate(k, n) for fam, (k, n) in sorted(by_fam.items())}
        vsum["verdict_correct_groups"] = len(strict_correct)
        vsum["verdict_correct_but_a_cited_value_invalid"] = lucky
        vsum["verdict_AND_values_correct"] = rate(len(strict_correct) - len(lucky), committed_det)

        # lenient basis: CANDIDATE is right if both cited values are genuine and differ
        cand = [c for c in committed if c["outcome"] == "CANDIDATE" and c["check_id"] in per_group]
        len_tp = [c["check_id"] for c in cand if all(is_valid(j) for j in per_group[c["check_id"]].values())]
        vsum["lenient_basis_precision_of_CANDIDATE"] = rate(len(len_tp), len(cand))
        vsum["lenient_true_positive_groups"] = len_tp

    report = {
        "metric_tier": "SILVER_NEW_OBJECTS_PASS1",
        "not_comparable_with": "official public-gold n=6 metric (Tyumenskaya/Novoslobodskaya); do not average or gate on this",
        "annotation": {"annotator": "single annotator, same model family as the system under test", "second_review": "PENDING", "training_eligible": False},
        "pipeline": {obj: {k: s["meta"].get(k) for k in ("run_seconds", "live_fragments", "tagger_coverage", "tagger_budget", "env", "code_fingerprint")} for obj, s in snaps.items()},
        "labels": {"total": len(rows), "by_verdict": dict(Counter(r["verdict"] for r in rows)),
                   "by_stratum": dict(Counter(r["stratum"] for r in rows)), "blind": sum(bool(r.get("labelled_blind_to_current_mechanism_output")) for r in rows)},
        "committed_groups": {"total": len(committed_rows), "by_truth": dict(committed_by_truth),
                             "unlabeled_committed_check_ids": unlabeled_committed, "rows": committed_rows},
        "strict_basis": strict,
        "label_uncertainty_sensitivity": sensitivity,
        "evaluate_case10_strict": e10_out,
        "original_19": orig_summary,
        "value_level": vsum,
        "rows": [{k: r[k] for k in ("check_id", "verdict", "stratum", "in_original_19", "mech_outcome", "verdict_confidence", "labelled_blind_to_current_mechanism_output")} for r in rows],
    }
    return report


def main() -> None:
    report = score(load_jsonl(LABELS), load_snapshots(), load_jsonl(VALUE_REVIEW))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "silver_metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    strict, e10_out, orig_summary, vsum = report["strict_basis"], report["evaluate_case10_strict"], report["original_19"], report["value_level"]
    print(json.dumps({"labels": report["labels"], "committed": {k: report["committed_groups"][k] for k in ("total", "by_truth", "unlabeled_committed_check_ids")},
                      "strict": {k: v for k, v in strict.items() if k != "note"}, "e10": {k: e10_out[k] for k in ("finding_precision", "finding_recall", "finding_f1", "counts")},
                      "orig19": {k: v for k, v in orig_summary.items() if k != "per_check"}, "value": vsum}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
