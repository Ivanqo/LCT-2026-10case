"""Guards for the SILVER review of the 6 new objects (evaluation/silver_new_objects_pass1).

Two things are pinned here:
  * the shipped label file keeps the 5-status methodology contract (exact file+page evidence for every
    determinate verdict, never training-eligible, never claiming a second review it does not have);
  * evaluate_silver.score() computes the SILVER metric with the semantics the report relies on
    (abstention is not a false positive, NEEDS_REVIEW never enters the gold, an un-labeled committed
    group is surfaced instead of silently biasing precision).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "evaluation" / "silver_new_objects_pass1"))

import evaluate_silver  # noqa: E402

LABEL_FILE = REPO_ROOT / "evaluation" / "silver_labels" / "new_objects_review_pass1.jsonl"
VALUE_FILE = REPO_ROOT / "evaluation" / "silver_labels" / "new_objects_value_review_pass1.jsonl"
STATUSES = {"VIOLATION_PRESENT", "NO_VIOLATION", "UNLABELED", "NEEDS_REVIEW", "EXCLUDED"}


def _label(obj: str, code: str, verdict: str) -> dict:
    return {"check_id": f"OBJ-NEW-{obj}::{code}", "object_id": f"OBJ-NEW-{obj}", "object_code": obj, "parameter_code": code,
            "verdict": verdict, "stratum": "T", "in_original_19": False, "verdict_confidence": "HIGH",
            "labelled_blind_to_current_mechanism_output": True}


def _group(code: str, comparability: str, finding: str) -> dict:
    return {"parameter_code": code, "comparability_status": comparability, "finding_status": finding}


def _snap(obj: str, groups: list[dict]) -> dict:
    by_code: dict[str, list[dict]] = {}
    for g in groups:
        by_code.setdefault(g["parameter_code"], []).append(g)
    return {obj: {"groups": by_code, "meta": {"run_seconds": 1.0}}}


class SilverLabelFileContract(unittest.TestCase):
    def setUp(self):
        self.rows = [json.loads(l) for l in LABEL_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_file_is_not_empty_and_check_ids_are_unique(self):
        self.assertGreaterEqual(len(self.rows), 19)
        ids = [r["check_id"] for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_row_uses_the_five_status_scheme_and_stays_silver(self):
        for r in self.rows:
            self.assertIn(r["verdict"], STATUSES, r["check_id"])
            self.assertEqual(r["annotation_tier"], "SILVER")
            self.assertFalse(r["training_eligible"], r["check_id"])
            self.assertEqual(r["second_review_status"], "PENDING", r["check_id"])

    def test_determinate_verdicts_cite_exact_file_and_page_for_both_stages(self):
        for r in self.rows:
            if r["verdict"] not in evaluate_silver.DETERMINATE:
                continue
            for stage in ("pd_reference", "rd_reference"):
                self.assertTrue(r[stage], f"{r['check_id']} has no {stage}")
                for ref in r[stage]:
                    self.assertTrue(ref["file_id"] and ref["file"], r["check_id"])
                    self.assertIsInstance(ref["page"], int, r["check_id"])
                    self.assertGreaterEqual(ref["page"], 1)
            self.assertIsNotNone(r["pd_value"], r["check_id"])
            self.assertIsNotNone(r["rd_value"], r["check_id"])

    def test_equal_relation_implies_no_violation(self):
        for r in self.rows:
            if r["value_relation"] == "EQUAL":
                self.assertEqual(r["verdict"], "NO_VIOLATION", r["check_id"])
            if r["verdict"] == "VIOLATION_PRESENT":
                self.assertEqual(r["value_relation"], "DIFFERENT_TRIGGER_MET", r["check_id"])

    def test_original_19_from_the_tagger_session_are_all_covered(self):
        self.assertEqual(sum(1 for r in self.rows if r["in_original_19"]), 19)

    def test_value_review_rows_reference_existing_checks(self):
        if not VALUE_FILE.exists():
            self.skipTest("value review not generated yet")
        ids = {r["check_id"] for r in self.rows}
        for v in (json.loads(l) for l in VALUE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()):
            self.assertIn(v["check_id"], ids)
            self.assertIn(v["stage"], {"PD", "RD"})


class SilverScoring(unittest.TestCase):
    def test_crosstab_precision_recall_and_abstention_semantics(self):
        labels = [
            _label("X", "A", "VIOLATION_PRESENT"),   # mechanism flags it            -> TP
            _label("X", "B", "VIOLATION_PRESENT"),   # mechanism says fine           -> FN (committed)
            _label("X", "C", "VIOLATION_PRESENT"),   # mechanism abstains            -> miss, not FN-committed
            _label("X", "D", "NO_VIOLATION"),        # mechanism flags it            -> FP
            _label("X", "E", "NO_VIOLATION"),        # mechanism confirms negative   -> TN
            _label("X", "F", "NO_VIOLATION"),        # mechanism abstains            -> abstention, never a FP
            _label("X", "G", "NEEDS_REVIEW"),        # committed but not determinate -> outside the gold
        ]
        snaps = _snap("X", [
            _group("A", "COMPARABLE", "CANDIDATE"), _group("B", "COMPARABLE", "NEGATIVE_VERIFIED"),
            _group("C", "NOT_COMPARABLE", "MISSING_EVIDENCE"), _group("D", "COMPARABLE", "CANDIDATE"),
            _group("E", "COMPARABLE", "NEGATIVE_VERIFIED"), _group("F", "NOT_COMPARABLE", "NOT_COMPARABLE"),
            _group("G", "COMPARABLE", "CANDIDATE"),
        ])
        report = evaluate_silver.score(labels, snaps, [])
        strict = report["strict_basis"]
        self.assertEqual(strict["n_determinate"], 6)
        self.assertEqual(strict["precision_of_CANDIDATE"]["k"], 1)
        self.assertEqual(strict["precision_of_CANDIDATE"]["n"], 2)
        self.assertEqual(strict["npv_of_NEGATIVE_VERIFIED"]["n"], 2)
        self.assertEqual(strict["recall_of_violations_abstain_counts_as_miss"]["n"], 3)
        self.assertEqual(strict["recall_of_violations_among_committed"]["n"], 2)
        self.assertEqual(strict["false_positive_rate_on_no_violation_checks"]["n"], 3)
        e10 = report["evaluate_case10_strict"]["counts"]["finding_confusion"]
        self.assertEqual((e10["tp"], e10["fp"], e10["fn"], e10["tn"]), (1, 1, 2, 2))   # abstentions count as "not flagged"
        self.assertEqual(report["metric_tier"], "SILVER_NEW_OBJECTS_PASS1")
        self.assertFalse(report["annotation"]["training_eligible"])

    def test_committed_group_without_a_label_is_surfaced(self):
        snaps = _snap("X", [_group("A", "COMPARABLE", "CANDIDATE")])
        report = evaluate_silver.score([_label("X", "B", "NO_VIOLATION")], snaps, [])
        self.assertEqual(report["committed_groups"]["unlabeled_committed_check_ids"], ["OBJ-NEW-X::A"])
        self.assertEqual(report["committed_groups"]["by_truth"], {"UNLABELED": 1})

    def test_verdict_correct_but_wrong_values_is_counted_as_lucky(self):
        labels = [_label("X", "A", "NO_VIOLATION")]
        snaps = _snap("X", [_group("A", "COMPARABLE", "NEGATIVE_VERIFIED")])
        value_rows = [{"check_id": "OBJ-NEW-X::A", "stage": "PD", "judgement": "VALID"},
                      {"check_id": "OBJ-NEW-X::A", "stage": "RD", "judgement": "WRONG_ROW"}]
        report = evaluate_silver.score(labels, snaps, value_rows)
        vl = report["value_level"]
        self.assertEqual(vl["verdict_correct_but_a_cited_value_invalid"], ["OBJ-NEW-X::A"])
        self.assertEqual(vl["verdict_AND_values_correct"]["k"], 0)
        self.assertEqual(vl["verdict_AND_values_correct"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
