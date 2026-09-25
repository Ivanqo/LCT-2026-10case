"""Module 5 (TZ 9.5, free hypothesis search) -- logical-analysis approach.

Covers `app/domain/logical_analysis.py`: the rule table itself, the pure
condition/keyword helpers, the end-to-end upsert behaviour against a real
(sqlite, in-memory) EvidenceGroup table, and the wiring into
`official_evidence.create_official_evidence_groups` that makes this module
reachable at all (before this, the only code that ever produced a
`SUSPICION` finding, `_create_gold_fixture_groups`, had zero callers -- see
CASE10_TZ_COMPLIANCE_AUDIT.md, "Модуль 5").
"""
from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.db.models import EvidenceFragment, EvidenceGroup, LogicalRuleRecord, Param, Suspicion
from app.domain import official_evidence
from app.domain.evidence_groups import sweep_orphaned_evidence_groups
from app.domain.logical_analysis import (
    DISCOVERY_METHOD_LOGICAL_ANALYSIS,
    LOGICAL_RULES,
    MODEL_VERSION_LOGICAL_ANALYSIS,
    _condition_met,
    _first_number,
    _keyword_evidence_fragment,
    run_logical_analysis_module,
)
from app.domain.training_release import FORBIDDEN_TRAINING_LABELS


class LogicalRuleTableTests(unittest.TestCase):
    def test_at_least_three_active_rules_with_real_normative_citations(self):
        active = [r for r in LOGICAL_RULES if r.is_active]
        self.assertGreaterEqual(len(active), 3)
        for rule in active:
            self.assertTrue(rule.rule_id)
            self.assertTrue(rule.condition_param_code)
            self.assertIn(rule.condition_op, {">", ">=", "<", "<=", "=="})
            self.assertIn(rule.implied_check, {"PARAM_EVIDENCE_PRESENT", "TEXT_KEYWORD_PRESENT"})
            self.assertTrue(rule.normative_base.strip())
            self.assertIn("СП", rule.normative_base)
            self.assertIn(rule.criticality, {"КРИТИЧЕСКОЕ", "СУЩЕСТВЕННОЕ"})
            self.assertGreater(rule.confidence, 0)
            self.assertLess(rule.confidence, 1)
            if rule.implied_check == "PARAM_EVIDENCE_PRESENT":
                self.assertTrue(rule.implied_param_code)
            else:
                self.assertTrue(rule.implied_keywords)

    def test_rule_ids_are_unique(self):
        ids = [rule.rule_id for rule in LOGICAL_RULES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_suspicion_status_stays_out_of_forbidden_training_labels_untouched(self):
        # Not a new invariant this module adds -- confirms the pre-existing
        # guard in training_release.py still covers SUSPICION, since a bug in
        # this module producing a real EvidenceGroup row with that status is
        # exactly the kind of thing that guard exists to catch.
        self.assertIn("SUSPICION", FORBIDDEN_TRAINING_LABELS)


class PureHelperTests(unittest.TestCase):
    def test_first_number_parses_common_forms(self):
        self.assertEqual(_first_number("6 этажей"), 6.0)
        self.assertEqual(_first_number("12,5 м"), 12.5)
        self.assertEqual(_first_number("-3"), -3.0)
        self.assertEqual(_first_number(9), 9.0)
        self.assertIsNone(_first_number(None))
        self.assertIsNone(_first_number(""))
        self.assertIsNone(_first_number("нет данных"))

    def test_condition_met_supports_all_operators(self):
        self.assertTrue(_condition_met(">=", 6, 6))
        self.assertTrue(_condition_met(">", 1, 0))
        self.assertFalse(_condition_met(">", 0, 0))
        self.assertTrue(_condition_met("<", 1, 2))
        self.assertTrue(_condition_met("<=", 2, 2))
        self.assertTrue(_condition_met("==", 2, 2))
        with self.assertRaises(ValueError):
            _condition_met("!=", 1, 1)

    def test_keyword_search_matches_within_the_right_discipline_only(self):
        doc_ar = SimpleNamespace(discipline="AR")
        doc_kr = SimpleNamespace(discipline="KR")
        by_id = {1: doc_ar, 2: doc_kr}
        fragments = [
            SimpleNamespace(document_version_id=2, source_system="pdf_text_layer", text="Незадымляемая лестничная клетка тип Н1"),
            SimpleNamespace(document_version_id=1, source_system="pdf_text_layer", text="План этажа. Лестничная клетка типа Н1"),
        ]
        hit = _keyword_evidence_fragment(fragments, by_id, discipline="AR", keywords=("лестничная клетка типа н1",))
        self.assertIs(hit, fragments[1])

    def test_keyword_search_finds_nothing_when_only_the_wrong_discipline_has_it(self):
        doc_kr = SimpleNamespace(discipline="KR")
        by_id = {1: doc_kr}
        fragments = [SimpleNamespace(document_version_id=1, source_system="pdf_text_layer", text="незадымляемая лестничная клетка")]
        self.assertIsNone(_keyword_evidence_fragment(fragments, by_id, discipline="AR", keywords=("незадымляем",)))

    def test_keyword_search_never_reads_gold_learning_annotations(self):
        doc = SimpleNamespace(discipline="AR")
        by_id = {1: doc}
        fragments = [SimpleNamespace(document_version_id=1, source_system="learning_annotation", text="незадымляемая лестничная клетка")]
        self.assertIsNone(_keyword_evidence_fragment(fragments, by_id, discipline="AR", keywords=("незадымляем",)))

    def test_keyword_search_is_yo_and_case_insensitive(self):
        doc = SimpleNamespace(discipline="AR")
        by_id = {1: doc}
        fragments = [SimpleNamespace(document_version_id=1, source_system="pdf_text_layer", text="НЕЗАДЫМЛЁМАЯ лестничная клетка")]
        self.assertIsNotNone(_keyword_evidence_fragment(fragments, by_id, discipline="AR", keywords=("незадымлем",)))


def _param(param_id: int, code: str, *, matrix_version_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id=param_id, code=code, matrix_version_id=matrix_version_id, review_priority="MEDIUM")


class RunLogicalAnalysisModuleTests(unittest.TestCase):
    """Integration tests against a real (sqlite, in-memory) EvidenceGroup
    table -- mirrors the `UpsertGenericGroupTests` pattern in
    test_case10_generic_extraction.py."""

    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(
            id="proc-logical", project_id=1, organization_id=1, object_id="OBJ-SYNTHETIC",
            matrix_version="official-132-v1", dataset_version="synthetic-v1", upload_scenario="FULL",
        )

    def tearDown(self):
        self.db.close()

    def _seed_condition_group(self, condition_param, *, actual_value, comparability="COMPARABLE", group_key="generic:anchor", confidence=0.6):
        group = EvidenceGroup(
            process_id=self.process.id, project_id=1, organization_id=1, object_id=self.process.object_id,
            param_id=int(condition_param.id), group_key=group_key, evidence_basis_hash="h",
            matrix_version="v", model_version="m", dataset_version="d", comparison_scenario="FULL",
            completeness_status="COMPLETE", comparability_status=comparability, finding_status="NEGATIVE_VERIFIED",
            actual_value=actual_value, expected_value=actual_value, confidence=confidence,
        )
        self.db.add(group)
        self.db.flush()
        self.db.add(EvidenceFragment(
            evidence_group_id=group.id, document_version_id=1, stage="working",
            document_code="25-01-PZ", page=4, role="actual", extracted_value=actual_value,
        ))
        self.db.flush()
        return group

    def test_fires_param_evidence_present_rule_when_implied_param_has_no_comparable_evidence(self):
        floors = _param(1, "PZ-007")
        elevator = _param(2, "KR-064")
        self._seed_condition_group(floors, actual_value="9")
        touched: dict[int, set[str]] = {}
        fired = run_logical_analysis_module(self.db, self.process, [floors, elevator], touched, fragments=[], by_id={})
        self.assertIn("LR-001", fired)
        group = self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-001").one()
        self.assertEqual(group.finding_status, "SUSPICION")
        self.assertEqual(group.comparability_status, "COMPARABLE")
        self.assertEqual(group.model_version, MODEL_VERSION_LOGICAL_ANALYSIS)
        self.assertEqual(group.delta["matrix_scope"], "FREE_SEARCH")
        self.assertEqual(group.delta["discovery_method"], DISCOVERY_METHOD_LOGICAL_ANALYSIS)
        self.assertEqual(group.delta["inspector_status"], "PENDING")
        self.assertEqual(group.delta["rule_id"], "LR-001")
        self.assertIn("СП 54.13330", group.delta["normative_base"])
        self.assertEqual(group.delta["rd_reference"], "25-01-PZ, л.4")
        self.assertIn(int(floors.id), touched)
        self.assertIn("suspicion:logical:LR-001", touched[int(floors.id)])
        # A SUSPICION must never be mistaken for a decidable/violation status.
        self.assertNotEqual(group.finding_status, "CONFIRMED_VIOLATION")

    def test_does_not_fire_when_implied_param_has_comparable_evidence(self):
        floors = _param(1, "PZ-007")
        elevator = _param(2, "KR-064")
        self._seed_condition_group(floors, actual_value="9")
        self.db.add(EvidenceGroup(
            process_id=self.process.id, project_id=1, organization_id=1, object_id=self.process.object_id,
            param_id=int(elevator.id), group_key="generic:anchor", evidence_basis_hash="h2",
            matrix_version="v", model_version="m", dataset_version="d", comparison_scenario="FULL",
            completeness_status="COMPLETE", comparability_status="COMPARABLE", finding_status="NEGATIVE_VERIFIED",
        ))
        self.db.flush()
        # Also satisfy LR-002 (shares the same PZ-007 condition) so this test
        # isolates LR-001's behaviour instead of asserting away LR-002 too.
        doc_ar = SimpleNamespace(discipline="AR")
        fragments = [SimpleNamespace(document_version_id=10, source_system="pdf_text_layer", text="незадымляемая лестничная клетка тип н1")]
        fired = run_logical_analysis_module(self.db, self.process, [floors, elevator], {}, fragments=fragments, by_id={10: doc_ar})
        self.assertEqual(fired, [])
        self.assertIsNone(self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-001").first())

    def test_does_not_fire_when_condition_is_below_threshold(self):
        floors = _param(1, "PZ-007")
        elevator = _param(2, "KR-064")
        self._seed_condition_group(floors, actual_value="4")
        fired = run_logical_analysis_module(self.db, self.process, [floors, elevator], {}, fragments=[], by_id={})
        self.assertEqual(fired, [])

    def test_abstains_honestly_when_condition_value_is_unparseable(self):
        floors = _param(1, "PZ-007")
        self._seed_condition_group(floors, actual_value="нет данных")
        fired = run_logical_analysis_module(self.db, self.process, [floors], {}, fragments=[], by_id={})
        self.assertEqual(fired, [])

    def test_abstains_when_condition_param_has_no_evidence_group_at_all(self):
        floors = _param(1, "PZ-007")
        fired = run_logical_analysis_module(self.db, self.process, [floors], {}, fragments=[], by_id={})
        self.assertEqual(fired, [])

    def test_a_suspicion_group_never_feeds_another_rules_condition(self):
        # If some future rule's implied/condition codes ever collided with
        # PZ-007, a stray "suspicion:*" row must not be picked up as if it
        # were a real extraction. Seed only a suspicion-shaped row.
        floors = _param(1, "PZ-007")
        self.db.add(EvidenceGroup(
            process_id=self.process.id, project_id=1, organization_id=1, object_id=self.process.object_id,
            param_id=int(floors.id), group_key="suspicion:logical:LR-999", evidence_basis_hash="h",
            matrix_version="v", model_version="m", dataset_version="d", comparison_scenario="FULL",
            completeness_status="COMPLETE", comparability_status="COMPARABLE", finding_status="SUSPICION",
            actual_value="9",
        ))
        self.db.flush()
        fired = run_logical_analysis_module(self.db, self.process, [floors], {}, fragments=[], by_id={})
        self.assertEqual(fired, [])

    def test_rule_whose_condition_param_is_out_of_this_runs_scope_is_left_untouched(self):
        touched: dict[int, set[str]] = {}
        fired = run_logical_analysis_module(self.db, self.process, [], touched, fragments=[], by_id={})
        self.assertEqual(fired, [])
        self.assertEqual(touched, {})

    def test_implied_param_out_of_run_scope_is_still_looked_up_via_the_matrix(self):
        # Simulates an incrementally-scoped run: only PZ-007 is in `params`
        # this time, but KR-064 still exists in the active matrix in the DB.
        floors = _param(1, "PZ-007", matrix_version_id=7)
        self._seed_condition_group(floors, actual_value="10")
        self.db.add(Param(
            id=2, matrix_version_id=7, organization_id=1, code="KR-064",
            parameter_name="Привязки и габариты лифтовых шахт", is_active=True,
        ))
        self.db.flush()
        fired = run_logical_analysis_module(self.db, self.process, [floors], {}, fragments=[], by_id={})
        self.assertIn("LR-001", fired)

    def test_text_keyword_rule_fires_without_a_matching_fragment_and_not_with_one(self):
        floors = _param(1, "PZ-007")
        self._seed_condition_group(floors, actual_value="8")
        doc_ar = SimpleNamespace(discipline="AR")
        by_id = {10: doc_ar}
        no_match = [SimpleNamespace(document_version_id=10, source_system="pdf_text_layer", text="План этажа без упоминаний")]
        fired = run_logical_analysis_module(self.db, self.process, [floors], {}, fragments=no_match, by_id=by_id)
        self.assertIn("LR-002", fired)

        match = [SimpleNamespace(document_version_id=10, source_system="pdf_text_layer", text="Незадымляемая лестничная клетка типа Н1")]
        fired2 = run_logical_analysis_module(self.db, self.process, [floors], {}, fragments=match, by_id=by_id)
        self.assertNotIn("LR-002", fired2)

    def test_stale_suspicion_is_swept_once_the_condition_no_longer_holds(self):
        floors = _param(1, "PZ-007")
        elevator = _param(2, "KR-064")
        self._seed_condition_group(floors, actual_value="9")
        # What the real per-param mechanism loop in official_evidence.py
        # would already have recorded for the condition group itself.
        touched: dict[int, set[str]] = {int(floors.id): {"generic:anchor"}}
        run_logical_analysis_module(self.db, self.process, [floors, elevator], touched, fragments=[], by_id={})
        self.assertIsNotNone(self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-001").first())
        self.assertIsNotNone(self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-002").first())
        sweep_orphaned_evidence_groups(self.db, self.process, {int(floors.id), int(elevator.id)}, touched)
        self.assertIsNotNone(self.db.query(EvidenceGroup).filter_by(group_key="generic:anchor").first())

        stale = self.db.query(EvidenceGroup).filter_by(group_key="generic:anchor", param_id=int(floors.id)).one()
        stale.actual_value = "3"
        stale.expected_value = "3"
        self.db.add(stale)
        self.db.flush()

        touched2: dict[int, set[str]] = {int(floors.id): {"generic:anchor"}}
        fired = run_logical_analysis_module(self.db, self.process, [floors, elevator], touched2, fragments=[], by_id={})
        self.assertEqual(fired, [])
        sweep_orphaned_evidence_groups(self.db, self.process, {int(floors.id), int(elevator.id)}, touched2)
        self.assertIsNone(self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-001").first())
        self.assertIsNone(self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-002").first())

    def test_parking_ventilation_rule_fires_on_a_strictly_greater_than_zero_condition(self):
        parking = _param(3, "PZ-012")
        ventilation = _param(4, "IOS4-079")
        self._seed_condition_group(parking, actual_value="12")
        fired = run_logical_analysis_module(self.db, self.process, [parking, ventilation], {}, fragments=[], by_id={})
        self.assertIn("LR-003", fired)
        group = self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-003").one()
        self.assertEqual(group.review_priority, "HIGH")  # КРИТИЧЕСКОЕ -> HIGH
        self.assertIn("СП 113.13330", group.delta["normative_base"])

    def test_parking_rule_does_not_fire_when_there_are_zero_underground_spaces(self):
        parking = _param(3, "PZ-012")
        self._seed_condition_group(parking, actual_value="0")
        fired = run_logical_analysis_module(self.db, self.process, [parking], {}, fragments=[], by_id={})
        self.assertEqual(fired, [])

    # -- ТЗ-10 `Suspicions` table wiring -----------------------------------

    def test_firing_a_rule_materializes_a_suspicions_row(self):
        floors = _param(1, "PZ-007")
        elevator = _param(2, "KR-064")
        self._seed_condition_group(floors, actual_value="9")
        run_logical_analysis_module(self.db, self.process, [floors, elevator], {}, fragments=[], by_id={})
        group = self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-001").one()
        suspicion = self.db.query(Suspicion).filter_by(evidence_group_id=group.id).one()
        self.assertEqual(suspicion.process_id, self.process.id)
        self.assertEqual(suspicion.project_id, 1)
        self.assertEqual(suspicion.organization_id, 1)
        self.assertEqual(suspicion.discovery_method, DISCOVERY_METHOD_LOGICAL_ANALYSIS)
        self.assertEqual(suspicion.inspector_status, "PENDING")
        self.assertIn("СП 54.13330", suspicion.normative_base)
        self.assertEqual(suspicion.rd_reference, "25-01-PZ, л.4")
        self.assertAlmostEqual(suspicion.confidence, 0.55)

    def test_suspicions_row_is_cascade_deleted_when_the_group_is_swept(self):
        floors = _param(1, "PZ-007")
        elevator = _param(2, "KR-064")
        self._seed_condition_group(floors, actual_value="9")
        touched: dict[int, set[str]] = {int(floors.id): {"generic:anchor"}}
        run_logical_analysis_module(self.db, self.process, [floors, elevator], touched, fragments=[], by_id={})
        group = self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-001").one()
        self.assertIsNotNone(self.db.query(Suspicion).filter_by(evidence_group_id=group.id).first())

        # LR-001's condition param key is deliberately left out of `touched`
        # this time, so the sweep treats it as orphaned (no inspector decision
        # exists on a SUSPICION group -- see DECIDABLE_FINDING_STATUSES).
        sweep_orphaned_evidence_groups(self.db, self.process, {int(floors.id), int(elevator.id)}, {int(floors.id): {"generic:anchor"}})
        self.assertIsNone(self.db.query(EvidenceGroup).filter_by(group_key="suspicion:logical:LR-001").first())
        self.assertIsNone(self.db.query(Suspicion).filter_by(evidence_group_id=group.id).first())

    def test_db_is_active_false_overrides_the_code_level_default(self):
        floors = _param(1, "PZ-007")
        elevator = _param(2, "KR-064")
        self._seed_condition_group(floors, actual_value="9")
        self.db.add(LogicalRuleRecord(
            rule_id="LR-001", rule_name="x", condition="x", expected="x", is_active=False,
        ))
        self.db.flush()
        fired = run_logical_analysis_module(self.db, self.process, [floors, elevator], {}, fragments=[], by_id={})
        self.assertNotIn("LR-001", fired)

    def test_no_db_row_for_a_rule_falls_back_to_the_code_level_default(self):
        # No LogicalRuleRecord seeded at all in this in-memory DB -- the code
        # default (is_active=True) must still apply, not "nothing fires".
        floors = _param(1, "PZ-007")
        elevator = _param(2, "KR-064")
        self._seed_condition_group(floors, actual_value="9")
        fired = run_logical_analysis_module(self.db, self.process, [floors, elevator], {}, fragments=[], by_id={})
        self.assertIn("LR-001", fired)


class WiringTests(unittest.TestCase):
    """Regression guard mirroring the existing dead-code check in
    test_case10_training_release.py (`_create_gold_fixture_groups` must have
    no caller) -- this module must have exactly the opposite property."""

    def test_official_evidence_calls_logical_analysis_before_its_final_sweep(self):
        source = inspect.getsource(official_evidence.create_official_evidence_groups)
        self.assertIn("run_logical_analysis_module", source)
        call_index = source.index("run_logical_analysis_module(db, process, params")
        sweep_index = source.index("sweep_orphaned_evidence_groups(db, process, {int(p.id) for p in params}")
        self.assertLess(call_index, sweep_index)


if __name__ == "__main__":
    unittest.main()
