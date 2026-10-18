"""Final round, step B1: output tiers, soft/hard gate reasons, candidate pairs of unresolved rooms, added systems,
the explanation / confidence contract and the no-result states."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.domain import comparison_gate as cg
from app.domain import evidence_tier as et

CODE = "IOS4-078"


class TierHelpersTests(unittest.TestCase):
    def test_a_plain_candidate_is_a_violation_candidate_and_gets_an_explanation(self):
        param = SimpleNamespace(unit="м", trigger_logic="parameter_id=1;trigger=Уменьшение ширины", sp_reference="СП 1", gost_reference=None, fz_reference=None)
        delta = et.finalize_delta(param, {"finding_status": "CANDIDATE", "expected_value": "10", "actual_value": "8", "confidence": 0.4,
                                          "delta": {"source": "generic_anchor_extractor"}})
        self.assertEqual(delta["evidence_tier"], et.TIER_VIOLATION_CANDIDATE)
        explanation = delta["explanation"]
        self.assertEqual((explanation["deviation_abs"], explanation["deviation_pct"]), (-2.0, 20.0))
        self.assertIn("СП 1", explanation["threshold_basis"])
        self.assertIsNone(explanation["probability"])
        self.assertIsNone(explanation["text_confidence"])          # not measured -> not invented
        self.assertIn("не вероятность", explanation["combined_confidence_kind"])

    def test_a_suggestion_needs_a_reason(self):
        with self.assertRaises(ValueError):
            et.suggestion({})
        delta = et.suggestion({}, "revision_conflict")
        self.assertEqual((delta["evidence_tier"], delta["tier_reasons"]), (et.TIER_SUGGESTION, ["revision_conflict"]))
        self.assertTrue(delta["tier_reason_text"][0])

    def test_set_differences_never_get_a_guessed_numeric_deviation(self):
        delta = et.finalize_delta(None, {"finding_status": "CANDIDATE", "expected_value": "V2", "actual_value": "V1",
                                         "delta": {"set_difference": True}})
        self.assertIsNone(delta["explanation"]["deviation_abs"])

    def test_no_result_states_are_distinct(self):
        cases = {
            ("MISSING_EVIDENCE", "missing_stage"): et.NO_DOCUMENTS,
            ("MISSING_EVIDENCE", "missing_discipline_document"): et.NO_DOCUMENTS,
            ("MISSING_EVIDENCE", "values_not_extracted"): et.NO_VALUE,
            ("MISSING_EVIDENCE", "no_relevant_evidence"): et.NO_VALUE,
            ("NOT_COMPARABLE", "unit_mismatch"): et.NOT_COMPARABLE,
            ("NOT_COMPARABLE", "LOW_QUALITY"): et.PROCESSING_ERROR,
            ("CLARIFICATION_REQUIRED", "CLARIFICATION_REQUIRED"): et.INCOMPLETE,
            ("NEGATIVE_VERIFIED", ""): et.NO_DIFFERENCE,
            ("CANDIDATE", ""): None,
        }
        for (status, reason), expected in cases.items():
            with self.subTest(status=status, reason=reason):
                self.assertEqual(et.no_result_state(status, reason), expected)
        self.assertEqual(len({v for v in cases.values() if v}), 6)

    def test_an_unreviewed_page_is_never_a_no_difference(self):
        # the only way to get NO_DIFFERENCE is NEGATIVE_VERIFIED; every abstention state differs from it
        for status in ("MISSING_EVIDENCE", "NOT_COMPARABLE", "CLARIFICATION_REQUIRED"):
            for reason in ("values_not_extracted", "LOW_QUALITY", "missing_stage", ""):
                self.assertNotEqual(et.no_result_state(status, reason), et.NO_DIFFERENCE)

    def test_queue_order_tier_then_priority_then_deviation(self):
        def key(status, tier_delta, priority, pct=None):
            delta = dict(tier_delta)
            if pct is not None:
                delta["explanation"] = {"deviation_pct": pct}
            return et.review_sort_key(status, delta, priority, None, "x")

        order = sorted([
            ("sug-high", key("CANDIDATE", {"evidence_tier": et.TIER_SUGGESTION}, "HIGH", 50)),
            ("viol-low", key("CANDIDATE", {}, "LOW", 1)),
            ("viol-high-small", key("CANDIDATE", {}, "HIGH", 1)),
            ("viol-high-big", key("CANDIDATE", {}, "HIGH", 40)),
            ("hyp", key("SUSPICION", {}, "HIGH")),
        ], key=lambda item: item[1])
        self.assertEqual([name for name, _ in order], ["viol-high-big", "viol-high-small", "viol-low", "sug-high", "hyp"])


class GateSoftHardTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(cg.SOFT_REASONS, {cg.REASON_REVISION_CONFLICT, cg.REASON_TRIGGER_NOT_EVALUABLE})
        self.assertFalse(cg.SOFT_REASONS & cg.HARD_REASONS)
        for reason in (cg.REASON_UNIT_MISMATCH, cg.REASON_MAGNITUDE, cg.REASON_DISCIPLINE_MISMATCH, cg.REASON_SELF_COMPARISON,
                       cg.REASON_MISSING_DISCIPLINE):
            self.assertTrue(reason in cg.HARD_REASONS)
            self.assertFalse(cg.GateDecision(cg.STATUS_NOT_COMPARABLE, reason).soft)
        self.assertTrue(cg.GateDecision(cg.STATUS_NOT_COMPARABLE, cg.REASON_REVISION_CONFLICT).soft)


class WiringTests(unittest.TestCase):
    """A soft gate decision turns a differing pair into a SUGGESTION; equal values stay an abstention."""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db.session import Base
        from tests.test_case10_comparison_gate import _clean_docs

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.docs = _clean_docs()
        self.process = SimpleNamespace(id="p", project_id=1, organization_id=1, object_id="OBJ-T", matrix_version="official-132-v1",
                                       dataset_version="v", upload_scenario="FULL")

    def tearDown(self):
        self.db.close()

    class _SoftGate:
        def evaluate(self, param, tier, observations):
            return cg.GateDecision(cg.STATUS_NOT_COMPARABLE, cg.REASON_REVISION_CONFLICT, {"stage": "PD", "conflicts": []})

    def _group(self, left, right):
        from app.db.models import EvidenceGroup, MatrixVersion, Param
        from app.domain import official_evidence as oe
        from tests.test_case10_comparison_gate import _obs

        matrix = MatrixVersion(organization_id=1, project_id=1, version="official-132-v1", status="ACTIVE", source_filename="t", source_hash="h")
        self.db.add(matrix)
        self.db.flush()
        param = Param(matrix_version_id=matrix.id, project_id=1, organization_id=1, code="PZ-004", matrix_code="PZ-004", scoring_code="PZ-004",
                      section="x", parameter_name="p", unit="м³", source_pd=True, source_rd=True, source_id=True,
                      trigger_logic="", review_priority="HIGH", other_normative="{}", data_type="number", is_active=True)
        self.db.add(param)
        self.db.flush()
        oe._upsert_generic_group(self.db, self.process, param, {"PD": _obs(self.docs[0], left), "RD": _obs(self.docs[2], right)},
                                 gate=self._SoftGate())
        return self.db.query(EvidenceGroup).one()

    def test_revision_conflict_with_a_difference_is_a_suggestion_with_its_reservation(self):
        group = self._group("100", "120")
        self.assertEqual(group.finding_status, "CANDIDATE")
        self.assertEqual(group.delta["evidence_tier"], "SUGGESTION")
        self.assertEqual(group.delta["tier_reasons"], ["revision_conflict"])
        self.assertEqual(group.delta["soft_gate"]["reason"], "revision_conflict")
        self.assertEqual((group.expected_value, group.actual_value), ("100", "120"))

    def test_revision_conflict_with_equal_values_is_not_a_no_violation(self):
        group = self._group("100", "100")
        self.assertEqual(group.finding_status, "NOT_COMPARABLE")
        self.assertEqual(group.delta["reason"], "revision_conflict")
        self.assertEqual(group.delta["no_result_state"], "NOT_COMPARABLE")


class RoomSuggestionTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db.models import DocumentVersion
        from app.db.session import Base

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        for doc_id in (10, 11, 12):
            self.db.add(DocumentVersion(id=doc_id, project_id=1, organization_id=1, source_type="t", filename=f"{doc_id}.pdf",
                                        dataset_file_id=f"F{doc_id}"))
        self.db.flush()
        self.documents = {i: self.db.get(DocumentVersion, i) for i in (10, 11, 12)}
        self.process = SimpleNamespace(id="p1", project_id=1, organization_id=1, object_id="O", matrix_version="m",
                                       dataset_version="d", upload_scenario="s")

    def tearDown(self):
        self.db.close()

    @staticmethod
    def _source(stage, doc, page, systems):
        return {"stage": stage, "document_id": doc, "page": page, "bbox_pdf": [10.0, 20.0, 110.0, 70.0],
                "page_width": 1000.0, "page_height": 500.0, "systems": systems, "floor": None, "building": None}

    def _row(self, pd_systems, rd_systems, basis):
        from app.domain.official_rule_packs import _candidate_pair

        pd_row = {"systems": pd_systems, **{}}
        rd_row = {"systems": rd_systems, "text_confidence": 0.9, "link_confidence": 0.6}
        pd_doc, rd_doc = self.documents[10], self.documents[12]
        pair = _candidate_pair(CODE, pd_row, pd_doc, rd_row, rd_doc, basis=basis)
        # sources need page/bbox: _issue_source reads them from the row's snapshot; patch them in for the test
        pair["PD"] = self._source("PD", 10, 3, pd_systems)
        pair["RD"] = self._source("RD", 12, 5, rd_systems)
        return pair

    def _issue(self, pairs, reason="AMBIGUOUS_ROOM_MATCH"):
        from app.domain.official_rule_packs import _with_pairs

        issue = {"code": CODE, "status": "CLARIFICATION_REQUIRED", "reason": reason, "location": "140", "stage": "RD", "text": "t",
                 "sources": [self._source("PD", 10, 3, ["V1"]), self._source("RD", 12, 5, ["V1"]), self._source("RD", 11, 6, [])]}
        return _with_pairs(issue, pairs)

    def _write(self, issues):
        from app.db.models import EvidenceFragment, EvidenceGroup
        from app.domain import official_evidence as oe

        param = SimpleNamespace(id=7, code=CODE, review_priority="HIGH", unit=None, trigger_logic=None)
        oe._upsert_room_identity_groups(self.db, self.process, param, issues, documents=self.documents)
        return self.db.query(EvidenceGroup).all(), self.db.query(EvidenceFragment).all()

    def test_a_discrepancy_in_one_candidate_pair_makes_a_suggestion_listing_all_pairs(self):
        pairs = [self._row(["V1", "V2"], ["V1"], "room_number_floor_unknown"),     # V2 disappeared here
                 self._row(["V1", "V2"], ["V1", "V2"], "room_number_floor_unknown")]
        self.assertTrue(pairs[0]["difference_visible"])
        self.assertFalse(pairs[1]["difference_visible"])
        groups, fragments = self._write([self._issue(pairs)])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.finding_status, "CANDIDATE")
        self.assertEqual(group.delta["evidence_tier"], "SUGGESTION")
        self.assertEqual(group.delta["tier_reasons"], ["AMBIGUOUS_ROOM_MATCH"])
        self.assertEqual((group.delta["candidate_pair_count"], group.delta["discrepant_pair_count"]), (2, 1))
        self.assertFalse(group.delta["violation_claimed"])
        self.assertEqual((group.expected_value, group.actual_value), ("V1,V2", "V1"))
        self.assertEqual(group.delta["explanation"]["missing_systems"], ["V2"])
        self.assertEqual(group.delta["explanation"]["link_confidence"], 0.6)
        self.assertTrue({f.page for f in fragments} >= {3, 5})

    def test_without_any_visible_discrepancy_it_stays_a_clarification_request(self):
        pairs = [self._row(["V1"], ["V1"], "pd_duplicate_key")]
        groups, _fragments = self._write([self._issue(pairs, "DUPLICATE_ROOM_KEY")])
        self.assertEqual(groups[0].finding_status, "CLARIFICATION_REQUIRED")
        self.assertEqual(groups[0].delta["no_result_state"], "INCOMPLETE_CHECK")

    def test_rd_systems_are_read_from_the_room_clip_and_level_marks_are_not_labels(self):
        from app.domain.official_rule_packs import _candidate_pair, _systems_in_room_clip

        words = [{"text": t, "bbox": [500 + i * 10, 400, 508 + i * 10, 410]} for i, t in enumerate(["В2.7", "V0.55", "В2.9"])]
        zone = {"snapshot": {"width": 1000, "height": 800, "page": 1, "words": words}, "room_bbox": [500, 400, 520, 410],
                "systems": [], "text_confidence": None, "link_confidence": None}
        self.assertEqual(_systems_in_room_clip(CODE, zone), (["V2.7", "V2.9"], "room_clip_text"))
        pair = _candidate_pair(CODE, {"systems": ["V2.7", "V2.8", "V2.9"]}, self.documents[10], zone, self.documents[12],
                               basis="room_number_floor_unknown")
        self.assertEqual((pair["rd_reading"], pair["missing_systems"], pair["difference_visible"]), ("room_clip_text", ["V2.8"], True))
        # nothing readable around the label: unknown, not a disappearance
        empty = {**zone, "snapshot": {**zone["snapshot"], "words": []}}
        self.assertEqual(_systems_in_room_clip(CODE, empty), ([], None))

    def test_an_unreadable_side_is_not_a_difference(self):
        pair = self._row(["V1"], [], "room_number_floor_unknown")
        self.assertIsNone(pair["rule_verdict"])
        self.assertFalse(pair["difference_visible"])

    def test_added_systems_078_is_a_suggestion_079_defers_to_its_rule_group(self):
        from app.domain import official_evidence as oe

        entry = {"code": CODE, "type": "added", "violation_claimed": False, "decision_mode": "disappearance", "location": "198",
                 "added_systems": ["V2"], "missing_systems": [], "pd_systems": ["V1"], "rd_systems": ["V1", "V2"],
                 "PD": self._source("PD", 10, 3, ["V1"]), "RD": self._source("RD", 12, 5, ["V1", "V2"]),
                 "text_confidence": None, "link_confidence": 0.7}
        param = SimpleNamespace(id=7, code=CODE, review_priority="HIGH", unit=None, trigger_logic=None)
        keys = oe._upsert_added_system_groups(self.db, self.process, param, [entry], documents=self.documents, existing_keys=set())
        self.assertEqual(keys, ["added:198"])
        from app.db.models import EvidenceGroup

        group = self.db.query(EvidenceGroup).one()
        self.assertEqual(group.delta["evidence_tier"], "SUGGESTION")
        self.assertEqual(group.delta["tier_reasons"], ["composition_change_not_decisive"])
        self.assertFalse(group.delta["violation_claimed"])
        self.assertEqual(group.delta["added_systems"], ["V2"])
        # (code, place) of the addition differs from the rule group's place "198": one row per (code, place) in the export
        self.assertEqual(group.delta["location"], "198 (добавлена система в РД)")
        self.assertEqual(group.delta["room_number"], "198")
        # 079: any change of the set is the trigger; the rule group already carries it
        param79 = SimpleNamespace(id=8, code="IOS4-079", review_priority="HIGH", unit=None, trigger_logic=None)
        entry79 = {**entry, "code": "IOS4-079", "decision_mode": "composition"}
        self.assertEqual(oe._upsert_added_system_groups(self.db, self.process, param79, [entry79], documents=self.documents,
                                                        existing_keys={"loc:198"}), [])
        written = oe._upsert_added_system_groups(self.db, self.process, param79, [entry79], documents=self.documents, existing_keys=set())
        self.assertEqual(written, ["added:198"])
        group79 = self.db.query(EvidenceGroup).filter(EvidenceGroup.group_key == "added:198", EvidenceGroup.param_id == 8).one()
        self.assertEqual(group79.delta["evidence_tier"], "VIOLATION_CANDIDATE")


class ExportAndProtocolTests(unittest.TestCase):
    def _group(self, **delta):
        return {"finding_status": "CANDIDATE", "review_priority": "HIGH", "parameter": {"code": "PZ-004", "name": "n"},
                "delta": {"parameter_code": "PZ-004", "location": "SITE", **delta}, "fragments": [], "id": 1}

    def test_export_carries_tier_confidences_and_deviation_separately(self):
        from evaluation.exporter import evidence_group_to_submission_check

        delta = {"evidence_tier": "SUGGESTION", "tier_reasons": ["revision_conflict"],
                 "explanation": {"rule": "r", "threshold": None, "threshold_basis": "b", "deviation_abs": -2.0, "deviation_pct": 20.0,
                                 "unit": "м", "deviation_basis": "x", "text_confidence": 0.9, "link_confidence": 0.5}}
        check = evidence_group_to_submission_check(self._group(**delta))
        self.assertEqual(check["violation_label"], "VIOLATION_PRESENT")      # a candidate for the inspector, tier says which kind
        self.assertEqual(check["evidence_tier"], "SUGGESTION")
        self.assertEqual((check["text_confidence"], check["link_confidence"]), (0.9, 0.5))
        self.assertEqual(check["deviation"], {"abs": -2.0, "pct": 20.0, "unit": "м", "basis": "x"})
        self.assertFalse(check["violation_claimed"] is True)
        plain = evidence_group_to_submission_check(self._group())
        self.assertEqual(plain["evidence_tier"], "VIOLATION_CANDIDATE")
        self.assertTrue(plain["violation_claimed"])
        self.assertIsNone(plain["deviation"])
        self.assertNotIn("probability", plain)

    def test_a_result_less_group_exports_its_state_and_reason_code(self):
        from evaluation.exporter import evidence_group_to_submission_check

        group = {"finding_status": "MISSING_EVIDENCE", "parameter": {"code": "PZ-004"}, "delta": {"parameter_code": "PZ-004", "reason": "missing_stage"},
                 "fragments": []}
        check = evidence_group_to_submission_check(group)
        self.assertEqual((check["no_result_state"], check["reason_code"], check["evidence_tier"]), ("NO_DOCUMENTS", "missing_stage", None))

    def test_annex_orders_candidates_by_tier_and_counts_them(self):
        from app.domain.protocol_annex2 import annex2_payload

        def row(ident, tier, priority):
            delta = {"parameter_code": "PZ-004", "location": str(ident), **({"evidence_tier": tier} if tier else {})}
            return {"id": ident, "finding_status": "CANDIDATE", "review_priority": priority, "inspector_status": "PENDING",
                    "parameter": {"code": "PZ-004", "name": "n", "review_priority": priority}, "delta": delta, "fragments": [], "decisions": [],
                    "confidence": None}

        findings = [row(1, "SUGGESTION", "HIGH"), row(2, None, "LOW"), row(3, None, "HIGH")]
        annex = annex2_payload(findings, [], object_info={}, matrix_size=132)
        self.assertEqual([c["evidence_group_id"] for c in annex["pending_candidates"]], [3, 2, 1])
        self.assertEqual(annex["evidence_tiers"], {"VIOLATION_CANDIDATE": 2, "SUGGESTION": 1, "HYPOTHESIS": 0})
        names = {s["name"]: s["count"] for s in annex["section_2_statistics"]}
        self.assertEqual(names["  из них предположений для верификации"], 1)


if __name__ == "__main__":
    unittest.main()
