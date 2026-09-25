"""Sensitivity of the SILVER metric to the live-tagger wall-clock budget (CASE10_LIVE_TAGGER_MAX_SECONDS).

The same code, same labels, three runs on this (shared, noisy) dev machine:
  run1  600 s budget, machine also busy with my own PDF-cache builders       (LOS3A/ALT79B/POL17 only)
  run2  600 s budget, otherwise idle                                          (LOS3A only)
  run3  7200 s budget (tagger runs to completion)  == mechanism_snapshots/   (all six)
Writes ../reports/new_objects_silver_pass1/sensitivity_tagger_budget.json
"""
import json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evaluate_silver as ev  # noqa: E402

labels = ev.load_jsonl(ev.LABELS)
value_rows = ev.load_jsonl(ev.VALUE_REVIEW)
RUNS = {"run1_600s_contended": HERE / "mechanism_snapshots_run1_contended",
        "run2_600s_idle": HERE / "mechanism_snapshots_run2_capped600s_LOS3A_only",
        "run3_7200s_uncapped": HERE / "mechanism_snapshots"}
out = {}
for name, d in RUNS.items():
    ev.SNAP_DIR = d
    snaps = ev.load_snapshots()
    for obj in snaps:                       # keep only objects present in this run so the label set matches
        pass
    sub_labels = [r for r in labels if r["object_code"] in snaps]
    rep = ev.score(sub_labels, snaps, [])
    s = rep["strict_basis"]
    out[name] = {
        "objects": sorted(snaps),
        "tagger_coverage": {o: snaps[o]["meta"].get("tagger_coverage") for o in snaps},
        "run_seconds": {o: snaps[o]["meta"].get("run_seconds") for o in snaps},
        "committed_groups": rep["committed_groups"]["total"],
        "crosstab": s["crosstab"], "precision_of_CANDIDATE": s["precision_of_CANDIDATE"], "npv_of_NEGATIVE_VERIFIED": s["npv_of_NEGATIVE_VERIFIED"],
        "coverage_of_determinate_checks": s["coverage_of_determinate_checks"],
        "committed_check_outcomes": {r["check_id"].replace("OBJ-NEW-", ""): r["mech_outcome"] for r in rep["rows"] if r["mech_outcome"] != "ABSTAIN"},
    }
(HERE.parent / "reports" / "new_objects_silver_pass1" / "sensitivity_tagger_budget.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
for name, o in out.items():
    print(name, o["objects"], "committed", o["committed_groups"], "prec", o["precision_of_CANDIDATE"]["k"], "/", o["precision_of_CANDIDATE"]["n"],
          "npv", o["npv_of_NEGATIVE_VERIFIED"]["k"], "/", o["npv_of_NEGATIVE_VERIFIED"]["n"], "cov", o["coverage_of_determinate_checks"]["k"], "/", o["coverage_of_determinate_checks"]["n"])
    print("   ", o["committed_check_outcomes"])
