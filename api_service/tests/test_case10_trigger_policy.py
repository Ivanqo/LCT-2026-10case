"""Trigger policy (Phase 10, prompt B, item 3): one typed row per catalog trigger, and its evaluation.

Every `kind` has at least one test below. The table is built from the catalog's trigger WORDING only; no
gold label is read anywhere in this file.
"""
from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.domain import trigger_policy as tp
from app.domain.official_rule_packs import SUPPORTED_RULE_CODES

D = Decimal
REPO = Path(__file__).resolve().parents[2]
_CATALOG = next(iter((REPO / "case_data").rglob("parameter_catalog_132.jsonl")), None) if (REPO / "case_data").is_dir() else None


def _rule(code: str) -> tp.TriggerRule:
    return tp.TRIGGER_POLICY[code]


class PolicyTableTests(unittest.TestCase):
    def test_table_has_exactly_the_132_catalog_codes_and_every_kind_is_known(self):
        self.assertEqual(len(tp.TRIGGER_POLICY), 132)
        self.assertTrue({r.kind for r in tp.TRIGGER_POLICY.values()} <= set(tp.TRIGGER_KINDS))

    def test_every_kind_is_used_at_least_once(self):
        counts = tp.kind_counts()
        self.assertTrue(all(counts[kind] > 0 for kind in tp.TRIGGER_KINDS), counts)

    def test_every_row_keeps_the_verbatim_catalog_text(self):
        self.assertTrue(all(r.source_text.strip() for r in tp.TRIGGER_POLICY.values()))

    @unittest.skipIf(_CATALOG is None, "catalog not available")
    def test_embedded_text_has_not_drifted_from_the_catalog(self):
        rows = [json.loads(line) for line in _CATALOG.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(tp.verify_against_catalog(rows), [])

    def test_threshold_and_order_rows_are_fully_specified(self):
        for rule in tp.TRIGGER_POLICY.values():
            if rule.kind == tp.ABS_THRESHOLD:
                self.assertIn(rule.op, {"<", "<=", ">", ">="})
                self.assertIsNotNone(rule.threshold)
                self.assertTrue(rule.threshold_unit)
            if rule.kind == tp.ENUM_DOWNGRADE:
                self.assertTrue(rule.order or rule.numeric_order, rule.code)

    def test_ambiguous_triggers_are_flagged_for_review(self):
        # multi-clause / range / two-sided wording must never look "settled"
        for code in ("PZ-020", "SPZU-031", "AR-042", "AR-047", "AR-048", "POD-094", "PPM-102", "ODI-116", "POS-084"):
            self.assertTrue(_rule(code).needs_review, code)

    def test_range_thresholds_use_the_bound_that_keeps_recall(self):
        # "менее 1.5-1.8 м": fires below the LARGER bound, so no candidate the catalog would call one is lost
        self.assertEqual(_rule("AR-047").threshold, D("1.8"))
        self.assertEqual(_rule("SPZU-031").threshold, D("12"))
        self.assertEqual(_rule("POS-084").threshold, D("4.5"))

    def test_rule_pack_rows_are_marked_informational(self):
        for code in SUPPORTED_RULE_CODES:
            self.assertTrue(_rule(code).applies_to_rule_pack, code)

    def test_unknown_code_degrades_to_conservative_any_change_with_review_flag(self):
        rule = tp.rule_for("X-999", trigger_logic="parameter_id=9;trigger=Что-то изменилось")
        self.assertEqual((rule.kind, rule.pct, rule.needs_review), (tp.ANY_CHANGE_PCT, D(0), True))
        self.assertEqual(rule.source_text, "Что-то изменилось")

    def test_trigger_text_is_read_from_official_trigger_logic(self):
        logic = "parameter_id=1;matrix_row=2;scoring_code=PZ-001;mapping_status=SOURCE_MATRIX;trigger=Расхождение > 0."
        self.assertEqual(tp.trigger_text_from_logic(logic), "Расхождение > 0.")

    def test_markdown_export_lists_every_row(self):
        self.assertEqual(len(tp.render_markdown().splitlines()), 132 + 2)


class NumericKindTests(unittest.TestCase):
    def test_decrease_fires_only_on_a_decrease(self):
        rule = _rule("KR-059")  # DECREASE
        self.assertEqual(tp.evaluate_numeric(rule, D(200), D(180)).status, tp.FIRES)
        self.assertEqual(tp.evaluate_numeric(rule, D(200), D(220)).status, tp.NOT_FIRING)

    def test_decrease_ignores_rounding_jitter(self):
        self.assertEqual(tp.evaluate_numeric(_rule("KR-059"), D("1000.00"), D("999.99")).status, tp.NOT_FIRING)

    def test_increase_fires_only_on_an_increase(self):
        rule = _rule("PZ-008")  # INCREASE
        self.assertEqual(tp.evaluate_numeric(rule, D("80"), D("83.5")).status, tp.FIRES)
        self.assertEqual(tp.evaluate_numeric(rule, D("80"), D("79")).status, tp.NOT_FIRING)

    def test_increase_with_a_percent_floor(self):
        rule = _rule("POS-082")  # INCREASE > 10 %
        self.assertEqual(tp.evaluate_numeric(rule, D(100), D(108)).status, tp.NOT_FIRING)
        self.assertEqual(tp.evaluate_numeric(rule, D(100), D(115)).status, tp.FIRES)

    def test_any_change_percent_has_a_strict_threshold(self):
        rule = _rule("SPZU-024")  # ANY_CHANGE_PCT(5)
        self.assertEqual(tp.evaluate_numeric(rule, D(1491), D(1350)).status, tp.FIRES)      # -9.5 %
        self.assertEqual(tp.evaluate_numeric(rule, D(1491), D(1420)).status, tp.NOT_FIRING)  # -4.8 %
        self.assertEqual(tp.evaluate_numeric(rule, D(1000), D(1050)).status, tp.NOT_FIRING)  # exactly 5 %

    def test_any_change_zero_percent_is_any_real_change(self):
        rule = _rule("PZ-004")
        self.assertEqual(tp.evaluate_numeric(rule, D(88264), D(88000)).status, tp.FIRES)
        self.assertEqual(tp.evaluate_numeric(rule, D(88264), D(88264)).status, tp.NOT_FIRING)

    def test_abs_threshold_value_less_than(self):
        rule = _rule("AR-041")  # RD/ID width < 0.9 m
        self.assertEqual(tp.evaluate_numeric(rule, D("1.0"), D("0.8")).status, tp.FIRES)
        self.assertEqual(tp.evaluate_numeric(rule, D("1.0"), D("1.2")).status, tp.NOT_FIRING)

    def test_abs_threshold_greater_than(self):
        rule = _rule("ODI-118")  # threshold height > 0.014 m
        self.assertEqual(tp.evaluate_numeric(rule, D("0.010"), D("0.020")).status, tp.FIRES)
        self.assertEqual(tp.evaluate_numeric(rule, D("0.020"), D("0.010")).status, tp.NOT_FIRING)

    def test_abs_threshold_on_the_change_itself(self):
        rule = _rule("SPZU-034")  # displacement > 0.5 m
        self.assertEqual(tp.evaluate_numeric(rule, D("10.0"), D("10.8")).status, tp.FIRES)
        self.assertEqual(tp.evaluate_numeric(rule, D("10.0"), D("9.7")).status, tp.NOT_FIRING)

    def test_abs_threshold_with_another_unit_is_undetermined_not_a_guess(self):
        # recall first: a value in mm against a threshold in m keeps the candidate instead of guessing a scale
        result = tp.evaluate_numeric(_rule("AR-041"), D("1.0"), D("800"), value_unit="мм")
        self.assertEqual(result.status, tp.UNDETERMINED)
        self.assertEqual(result.detail["reason"], "unit_mismatch")

    def test_abs_threshold_magnitude_guard(self):
        result = tp.evaluate_numeric(_rule("AR-041"), D("1.0"), D("900"))
        self.assertEqual(result.status, tp.UNDETERMINED)
        self.assertEqual(result.detail["reason"], "magnitude_out_of_range")

    def test_presence_fires_when_the_element_disappears(self):
        rule = _rule("ODI-120")
        self.assertEqual(tp.evaluate_numeric(rule, D(4), D(0)).status, tp.FIRES)
        self.assertEqual(tp.evaluate_numeric(rule, D(4), D(2)).status, tp.NOT_FIRING)
        self.assertEqual(tp.evaluate_numeric(rule, D(0), D(0)).status, tp.NOT_FIRING)

    def test_count_change_fires_on_any_different_count(self):
        rule = _rule("PZ-010")
        self.assertEqual(tp.evaluate_numeric(rule, D(120), D(118)).status, tp.FIRES)
        self.assertEqual(tp.evaluate_numeric(rule, D(120), D(122)).status, tp.FIRES)
        self.assertEqual(tp.evaluate_numeric(rule, D(120), D(120)).status, tp.NOT_FIRING)

    def test_not_evaluable_never_fires_and_says_so(self):
        result = tp.evaluate_numeric(_rule("POS-081"), D(10), D(50))
        self.assertEqual(result.status, tp.NOT_EVALUABLE_STATUS)

    def test_the_hard_negative_that_motivated_the_table(self):
        # KR-058-style: RD plate THICKER than PD is NO_VIOLATION under a decrease trigger
        self.assertEqual(tp.evaluate_numeric(_rule("KR-058"), D(1000), D(1200)).status, tp.NOT_FIRING)


class EnumKindTests(unittest.TestCase):
    def test_list_order_downgrade(self):
        rule = _rule("PZ-022")  # I best ... V worst
        self.assertEqual(tp.evaluate_enum(rule, "I", "II").status, tp.FIRES)
        self.assertEqual(tp.evaluate_enum(rule, "II", "I").status, tp.NOT_FIRING)
        self.assertEqual(tp.evaluate_enum(rule, "II", "II").status, tp.NOT_FIRING)

    def test_hazard_class_order(self):
        rule = _rule("PZ-023")
        self.assertEqual(tp.evaluate_enum(rule, "C0", "C1").status, tp.FIRES)
        self.assertEqual(tp.evaluate_enum(rule, "C1", "C0").status, tp.NOT_FIRING)

    def test_material_class_order_and_energy_order(self):
        self.assertEqual(tp.evaluate_enum(_rule("PPM-107"), "KM1", "KM3").status, tp.FIRES)
        self.assertEqual(tp.evaluate_enum(_rule("PZ-021"), "A", "B").status, tp.FIRES)
        self.assertEqual(tp.evaluate_enum(_rule("PZ-021"), "A++", "A").status, tp.FIRES)
        self.assertEqual(tp.evaluate_enum(_rule("PZ-021"), "B", "A").status, tp.NOT_FIRING)

    def test_numeric_grade_order(self):
        rule = _rule("KR-056")  # higher number = stronger
        self.assertEqual(tp.evaluate_enum(rule, "C345", "C245").status, tp.FIRES)
        self.assertEqual(tp.evaluate_enum(rule, "C245", "C345").status, tp.NOT_FIRING)
        self.assertEqual(tp.evaluate_enum(_rule("KR-057"), "A500C", "A400").status, tp.FIRES)
        self.assertEqual(tp.evaluate_enum(_rule("KR-057"), "A-III", "A-II").status, tp.FIRES)

    def test_unknown_value_is_undetermined_not_a_guess(self):
        result = tp.evaluate_enum(_rule("PZ-022"), "II", "IX")
        self.assertEqual(result.status, tp.UNDETERMINED)

    def test_any_change_on_an_enum_fires(self):
        self.assertEqual(tp.evaluate_enum(_rule("PZ-001"), "a", "b").status, tp.FIRES)

    def test_ordering_rule_on_a_plain_number_is_undetermined(self):
        self.assertEqual(tp.evaluate_numeric(_rule("PZ-022"), D(1), D(2)).status, tp.UNDETERMINED)


class CompoundAndAggregateTests(unittest.TestCase):
    def test_component_wise_any_change_percent(self):
        rule = _rule("KR-067")  # > 2 %
        result = tp.evaluate_components(rule, ["м³", "т"], [D(1000), D(50)], [D(1010), D(50)])
        self.assertEqual(result.status, tp.NOT_FIRING)  # +1 % on the first, none on the second
        result = tp.evaluate_components(rule, ["м³", "т"], [D(1000), D(50)], [D(1010), D(60)])
        self.assertEqual(result.status, tp.FIRES)       # +20 % on the second

    def test_abs_threshold_is_applied_only_to_the_component_with_its_unit(self):
        rule = _rule("ODI-119")  # < 1.5 м
        fires = tp.evaluate_components(rule, ["м²", "м"], [D("3.0"), D("1.6")], [D("3.0"), D("1.4")])
        self.assertEqual(fires.status, tp.FIRES)
        # the area component (1.2 m2) is not judged against a width bound
        quiet = tp.evaluate_components(rule, ["м²", "м"], [D("3.0"), D("1.6")], [D("1.2"), D("1.7")])
        self.assertEqual(quiet.status, tp.NOT_FIRING)

    def test_no_component_with_the_threshold_unit_is_undetermined(self):
        result = tp.evaluate_components(_rule("ODI-119"), ["шт.", "компл."], [D(1), D(1)], [D(0), D(2)])
        self.assertEqual(result.status, tp.UNDETERMINED)

    def test_aggregate_prefers_fires_then_undetermined_then_not_firing(self):
        rule = _rule("PZ-004")
        fire = tp.evaluate_numeric(rule, D(10), D(20))
        quiet = tp.evaluate_numeric(rule, D(10), D(10))
        unknown = tp.evaluate_numeric(_rule("PZ-022"), D(1), D(2))
        self.assertEqual(tp.aggregate(rule, [quiet, fire]).status, tp.FIRES)
        self.assertEqual(tp.aggregate(rule, [quiet, unknown]).status, tp.UNDETERMINED)
        self.assertEqual(tp.aggregate(rule, [quiet, quiet]).status, tp.NOT_FIRING)

    def test_not_evaluable_wins_the_aggregate(self):
        rule = _rule("POS-081")
        ev = tp.evaluate_numeric(rule, D(1), D(2))
        self.assertEqual(tp.aggregate(rule, [ev]).status, tp.NOT_EVALUABLE_STATUS)

    def test_keep_candidate_predicate(self):
        rule = _rule("PZ-004")
        self.assertTrue(tp.evaluate_numeric(rule, D(10), D(20)).fires_or_unknown)
        self.assertFalse(tp.evaluate_numeric(rule, D(10), D(10)).fires_or_unknown)


class WiringTests(unittest.TestCase):
    """The trigger is applied inside the generic tiers' groups (official_evidence._verdict)."""

    def test_verdict_helper(self):
        from app.domain.official_evidence import _verdict

        self.assertEqual(_verdict(True, None), ("NEGATIVE_VERIFIED", {}))
        fired = tp.evaluate_numeric(_rule("PZ-004"), D(1), D(2))
        status, extra = _verdict(False, fired)
        self.assertEqual(status, "CANDIDATE")
        self.assertEqual(extra["trigger"]["evaluation"], tp.FIRES)
        quiet = tp.evaluate_numeric(_rule("KR-059"), D(200), D(220))
        status, extra = _verdict(False, quiet)
        self.assertEqual(status, "NEGATIVE_VERIFIED")
        self.assertEqual(extra["reason"], "change_not_triggering")
        self.assertIn("source_text", extra["trigger"])
        unknown = tp.evaluate_numeric(_rule("PZ-022"), D(1), D(2))
        self.assertEqual(_verdict(False, unknown)[0], "CANDIDATE")  # undecidable -> recall is kept

    def _observation(self, value: str, doc_id: int):
        from app.domain.generic_matrix_extraction import GenericObservation

        return GenericObservation(
            value=value, normalized_value=value, decimal_value=D(value), confidence=0.55,
            document=SimpleNamespace(id=doc_id, dataset_file_id=f"F{doc_id}", file_hash=f"h{doc_id}", content_hash=None,
                                     discipline="AR", document_code=f"D{doc_id}", revision=None, approval_status="UNKNOWN"),
            page=1, bbox_normalized=[0.1, 0.1, 0.2, 0.2], bbox_pdf=[1.0, 1.0, 2.0, 2.0], page_width=100.0, page_height=100.0,
            extractor="generic_anchor_numeric", context="ctx", source_fragment=None,
        )

    def test_group_is_a_verified_negative_when_the_change_cannot_trigger(self):
        from app.db.models import EvidenceGroup
        from app.domain.official_evidence import _upsert_generic_group

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        process = SimpleNamespace(id="p", project_id=1, organization_id=1, object_id="OBJ-T", matrix_version="official-132-v1",
                                  dataset_version="v", upload_scenario="FULL")
        param = SimpleNamespace(id=1, code="KR-059", review_priority="HIGH", source_pd=True, source_rd=True, source_id=False, unit="мм")
        _upsert_generic_group(db, process, param, {"PD": self._observation("200", 1), "RD": self._observation("220", 2)})
        group = db.query(EvidenceGroup).one()
        self.assertEqual(group.finding_status, "NEGATIVE_VERIFIED")
        self.assertEqual(group.delta["reason"], "change_not_triggering")
        self.assertEqual(group.delta["comparison_result"], "NON_TRIGGERING")
        db.close()

    def test_group_stays_a_candidate_when_the_change_triggers(self):
        from app.db.models import EvidenceGroup
        from app.domain.official_evidence import _upsert_generic_group

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        process = SimpleNamespace(id="p", project_id=1, organization_id=1, object_id="OBJ-T", matrix_version="official-132-v1",
                                  dataset_version="v", upload_scenario="FULL")
        param = SimpleNamespace(id=1, code="KR-059", review_priority="HIGH", source_pd=True, source_rd=True, source_id=False, unit="мм")
        _upsert_generic_group(db, process, param, {"PD": self._observation("200", 1), "RD": self._observation("180", 2)})
        group = db.query(EvidenceGroup).one()
        self.assertEqual(group.finding_status, "CANDIDATE")
        self.assertEqual(group.delta["comparison_result"], "TRIGGERED")
        self.assertEqual(group.delta["trigger"]["evaluation"], tp.FIRES)
        db.close()


if __name__ == "__main__":
    unittest.main()
