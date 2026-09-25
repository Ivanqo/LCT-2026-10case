"""Build + freeze the value corpus from the hand-written label modules.

  python -m evaluation.measurement_bench.build_corpus            build files, print coverage/verification report
  python -m evaluation.measurement_bench.build_corpus --freeze   also write corpus_manifest.json (sha256 + frozen_at)

Outputs (corpus/):
  value_corpus_v1.jsonl   one row per (object, parameter, stage) with a ground-truth value
  probe_log_v1.jsonl      every documented search attempt that produced NO scalar truth
  corpus_manifest.json    hash, counts, split composition, coverage of the seeded probe plan, frozen_at

Every downstream stage (run_pipeline, metric 1/2/3) refuses to start unless the
manifest exists and the corpus hash still matches -> "values recorded before the
extractor was run" is enforced, not just claimed.
"""
from __future__ import annotations

import argparse
import collections
import importlib
import json
import sys

from .common import CORPUS_DIR, OBJECTS, catalog_by_code, family_of_code, is_critical, norm_enum, norm_number, read_jsonl, sha256_file, utcnow_iso, write_jsonl

LABEL_MODULES = [
    "corpus_labels", "labels_okt103_izm12", "labels_doo25", "labels_los3a", "labels_public", "labels_compound", "labels_extra",
]
# The 19 (object, code) groups that the pass-1 mechanism run selected for the SILVER review.
GROUPS19 = {
    ("ALT79B", "KR-067"), ("ALT79B", "PZ-023"), ("ALT79B", "SPZU-024"), ("DOO25", "PZ-023"),
    ("LOS3A", "AR-041"), ("LOS3A", "KR-067"), ("LOS3A", "PZ-002"), ("LOS3A", "PZ-004"), ("LOS3A", "PZ-005"), ("LOS3A", "PZ-006"),
    ("LOS3A", "PZ-008"), ("LOS3A", "PZ-022"), ("LOS3A", "PZ-023"), ("LOS3A", "SPZU-024"),
    ("POL17", "AR-041"), ("POL17", "PZ-005"), ("POL17", "PZ-006"), ("POL17", "PZ-022"), ("POL17", "PZ-023"),
}
LABELLER = "claude-single-annotator (same model family as the system under test)"
LABEL_DATE = "2026-09-24"


def load_labels() -> tuple[list[dict], list[dict]]:
    for name in LABEL_MODULES:
        importlib.import_module(f"evaluation.measurement_bench.{name}")
    from . import corpus_labels

    return corpus_labels.LABELS, corpus_labels.PROBE_LOG


def norm_value(family: str, value):
    if isinstance(value, (list, tuple)):
        return [norm_number(v) for v in value]
    if family == "ENUM":
        return norm_enum(value)
    return norm_number(value)


def build() -> tuple[list[dict], list[dict], dict]:
    labels, probe_log = load_labels()
    cat = catalog_by_code()
    plan = json.loads((CORPUS_DIR / "probe_plan.json").read_text(encoding="utf-8"))
    planned = {(o, c): f for o, e in plan["attempts"].items() for f, cs in e.items() for c in cs}
    rows: list[dict] = []
    for l in labels:
        obj, code = l["obj"], l["code"]
        fam = family_of_code(code)
        assert fam in ("ENUM", "NUMERIC_TABLE", "COMPOUND", "TABLE_COUNT"), (obj, code, fam)
        info = OBJECTS[obj]
        if (obj, code) in planned:
            prov, by_sys = "PLAN", False
        elif (obj, code) in GROUPS19:
            prov, by_sys = "PASS1_MECHANISM_SELECTED", True
        else:
            prov, by_sys = "SUPPLEMENT", False
        main = norm_value(fam, l["value"])
        accepted = [main]
        locs = [{"file_id": l["file_id"], "page": l["page"], "value_printed": l["value"], "value_norm": main, "role": "primary"}]
        for a in l["alt"]:
            locs.append({"file_id": a["file_id"], "page": a["page"], "value_printed": a["value"], "value_norm": norm_value(fam, a["value"]), "role": "same_value"})
        any_norms = []
        for a in l["any_of"]:
            n = norm_value(fam, a["value"])
            any_norms.append(n)
            locs.append({"file_id": a["file_id"], "page": a["page"], "value_printed": a["value"], "value_norm": n, "role": "any_of", "note": a["note"]})
        for n in any_norms:
            if n not in accepted:
                accepted.append(n)
        sup = [{**s, "value_norm": norm_value(fam, s["value"])} for s in l["sup"]]
        rows.append({
            "triple_id": f"{obj}::{code}::{l['stage']}",
            "obj": obj, "object_id": info["object_id"], "split": info["split"], "cleanliness": info["cleanliness"],
            "param_code": code, "parameter_name": cat[code]["parameter_name"], "unit": cat[code].get("unit"), "family": fam,
            "critical": is_critical(cat[code]), "stage": l["stage"],
            "truth_status": "VALUE", "truth_kind": "SET_ANY" if len(accepted) > 1 else "SINGLE",
            "true_value_printed": l["value"], "true_norm": main, "accepted_norm": accepted,
            "file_id": l["file_id"], "page": l["page"], "line_text": l["line"],
            "locations": locs, "superseded_or_conflicting": sup,
            "confidence": l["conf"], "provenance": prov, "supplement_kind": l["supplement_kind"],
            "selected_by_system_output": by_sys, "note": l["note"],
            "annotation_tier": "SILVER", "labelled_by": LABELLER, "labelled_at": LABEL_DATE,
            "labelled_before_extractor_run": True, "second_review_status": "PENDING", "training_eligible": False,
        })
    rows.sort(key=lambda r: (r["obj"], r["param_code"], r["stage"]))
    log_rows = []
    for p in probe_log:
        fam = family_of_code(p["code"])
        log_rows.append({**p, "family": fam, "split": OBJECTS[p["obj"]]["split"], "planned": (p["obj"], p["code"]) in planned})
    # explicit NO_TEXT_LAYER rows carry prov AUTO; keep as-is
    have = {(r["obj"], r["param_code"]) for r in rows} | {(p["obj"], p["code"]) for p in probe_log}
    uncovered = sorted((o, c, f) for (o, c), f in planned.items() if (o, c) not in have)
    manifest_extra = {"plan_attempts": len(planned), "plan_uncovered": [list(x) for x in uncovered]}
    return rows, log_rows, manifest_extra


def summarize(rows: list[dict], log_rows: list[dict]) -> dict:
    c = collections.Counter
    hold = [r for r in rows if r["split"] == "holdout"]
    return {
        "n_triples": len(rows),
        "by_family": dict(c(r["family"] for r in rows)),
        "by_split": dict(c(r["split"] for r in rows)),
        "holdout_share": round(len(hold) / max(1, len(rows)), 4),
        "by_object": dict(c(r["obj"] for r in rows)),
        "by_stage": dict(c(r["stage"] for r in rows)),
        "by_family_split": {f"{fam}/{sp}": n for (fam, sp), n in sorted(c((r["family"], r["split"]) for r in rows).items())},
        "by_provenance": dict(c(r["provenance"] for r in rows)),
        "selected_by_system_output": sum(r["selected_by_system_output"] for r in rows),
        "truth_kind": dict(c(r["truth_kind"] for r in rows)),
        "critical_triples": sum(r["critical"] for r in rows),
        "distinct_critical_codes": len({r["param_code"] for r in rows if r["critical"]}),
        "probe_log_by_status": dict(c(p["status"] for p in log_rows)),
        "probe_log_planned_by_status": dict(c(p["status"] for p in log_rows if p["planned"])),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true")
    a = ap.parse_args()
    rows, log_rows, extra = build()
    write_jsonl(CORPUS_DIR / "value_corpus_v1.jsonl", rows)
    write_jsonl(CORPUS_DIR / "probe_log_v1.jsonl", log_rows)
    summary = summarize(rows, log_rows)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("plan attempts uncovered:", len(extra["plan_uncovered"]))
    for o, c_, f in extra["plan_uncovered"]:
        print("   UNCOVERED", o, f, c_)
    if a.freeze:
        if extra["plan_uncovered"]:
            print("refusing to freeze: probe plan not fully covered")
            return 2
        if summary["n_triples"] < 120 or summary["holdout_share"] < 0.6:
            print("refusing to freeze: need >=120 triples and >=60% holdout")
            return 3
        manifest = {
            "corpus_version": "v1", "frozen_at": utcnow_iso(),
            "corpus_sha256": sha256_file(CORPUS_DIR / "value_corpus_v1.jsonl"),
            "probe_log_sha256": sha256_file(CORPUS_DIR / "probe_log_v1.jsonl"),
            "probe_plan_sha256": sha256_file(CORPUS_DIR / "probe_plan.json"),
            "summary": summary, **extra,
            "statement": "All values were read from the original PDFs (reader.py/probe_object.py, no pipeline involved) and written before any extractor "
                         "was run on this corpus. Downstream stages verify corpus_sha256 and that their own outputs are created after frozen_at.",
        }
        (CORPUS_DIR / "corpus_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        print("FROZEN", manifest["frozen_at"], manifest["corpus_sha256"][:16])
    return 0


if __name__ == "__main__":
    sys.exit(main())
