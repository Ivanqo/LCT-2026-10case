"""Final / step C1: the inspector card against the experts' checklist -- sheet designation, stage provenance, the six
"no result" states, per-part decisions on compound findings, the explanation contract, hypothesis card, progress
panel and the protocol entry. Pure functions plus endpoint-level (TestClient) checks on the synthetic dataset."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from app.db.models import AuditLog, EvidenceDecision, EvidenceGroup, InspectionProcess, InspectorEdit
from app.domain import inspector_card as card
from test_case10_s5_workbench import WorkbenchTestBase


def _word(x, y, text, w=0.02, h=0.012, width=2000.0, height=1000.0):
    return (x * width, y * height, (x + w) * width, (y + h) * height, text)


class SheetDesignationTests(unittest.TestCase):
    def test_sheet_number_and_cipher_come_from_the_title_block(self):
        words = [
            _word(0.79, 0.91, "Изм."), _word(0.81, 0.91, "Лист"),       # revision table header: no "Листов" beside it
            _word(0.81, 0.93, "3"),                                        # a revision row, must not be taken as the sheet
            _word(0.96, 0.92, "Лист"), _word(0.98, 0.92, "Листов"), _word(0.96, 0.94, "10"),
            _word(0.9, 0.86, "АНО/150321/1-П-ИОС5.4.2", w=0.08),
        ]
        found = card.extract_sheet_designation(words, 2000.0, 1000.0)
        self.assertEqual(found["sheet"], "10")
        self.assertEqual(found["cipher"], "АНО/150321/1-П-ИОС5.4.2")
        self.assertEqual(found["label"], "Лист 10 · АНО/150321/1-П-ИОС5.4.2")

    def test_page_without_a_title_block_has_no_designation_instead_of_a_guess(self):
        words = [_word(0.1, 0.1, "Экспликация"), _word(0.2, 0.2, "Лист"), _word(0.2, 0.22, "5"), _word(0.51, 0.78, "193")]
        self.assertIsNone(card.extract_sheet_designation(words, 2000.0, 1000.0))
        self.assertIsNone(card.extract_sheet_designation([], 2000.0, 1000.0))

    def test_rotated_page_words_are_mapped_to_the_visible_frame(self):
        import fitz

        doc = fitz.open()
        page = doc.new_page(width=600, height=300)
        page.insert_text((20, 40), "HELLO")
        page.set_rotation(90)
        words = card._visible_words(page)
        self.assertEqual(len(words), 1)
        x0, y0, x1, y1, text = words[0]
        self.assertEqual(text, "HELLO")
        self.assertTrue(0 <= x0 < x1 <= page.rect.width and 0 <= y0 < y1 <= page.rect.height)


class StageProvenanceTests(unittest.TestCase):
    def doc(self, filename, doc_stage=None, dataset_stage=None):
        return SimpleNamespace(filename=filename, doc_stage=doc_stage, dataset_stage=dataset_stage)

    def test_file_name_that_contradicts_the_panel_is_flagged(self):
        info = card.stage_info(self.doc("Исполнительный чертеж ОВ.pdf", doc_stage="working", dataset_stage="RD"), "RD")
        self.assertTrue(info["conflict"])
        self.assertIn("ИД", info["warning"])
        self.assertEqual([s["basis"] for s in info["sources"]][:2], ["реестр файлов", "классификация документа"])

    def test_registry_and_classification_agree_with_the_panel(self):
        info = card.stage_info(self.doc("АНО-1-РД-ОВ1.pdf", doc_stage="working", dataset_stage="RD"), "RD")
        self.assertFalse(info["conflict"])
        self.assertIsNone(info["warning"])

    def test_registry_disagreeing_with_the_panel_is_flagged(self):
        info = card.stage_info(self.doc("plan.pdf", doc_stage="project", dataset_stage="PD"), "RD")
        self.assertTrue(info["conflict"])

    def test_unknown_stage_is_reported_as_undetermined(self):
        info = card.stage_info(self.doc("scan.pdf"), "RD")
        self.assertFalse(info["conflict"])
        self.assertIsNone(info["sources"][0]["stage"])


class NoResultStateTests(unittest.TestCase):
    def state(self, status, **kwargs):
        base = dict(finding_status=status, comparability_status="COMPARABLE", delta={}, visible_stages=["PD", "RD"],
                    low_quality=False, has_fragments=True)
        base.update(kwargs)
        return card.no_result_state(**base)

    def test_candidates_confirmed_and_hypotheses_have_a_result_to_judge(self):
        for status in ("CANDIDATE", "CONFIRMED_VIOLATION", "SUSPICION"):
            self.assertIsNone(self.state(status))

    def test_six_states_with_reason_and_action(self):
        no_docs = self.state("MISSING_EVIDENCE", delta={"reason": "missing_discipline_document", "gate": {"stage": "RD"}}, visible_stages=["PD"], has_fragments=False)
        no_value = self.state("MISSING_EVIDENCE", delta={"reason": "value_not_extracted"})
        not_comparable = self.state("NOT_COMPARABLE", comparability_status="COMPARISON_IMPOSSIBLE", delta={"reason": "revision_conflict"})
        error = self.state("MISSING_EVIDENCE", low_quality=True)
        incomplete = self.state("CLARIFICATION_REQUIRED", delta={"reason": "mixed_stage_requires_review"})
        clear = self.state("NEGATIVE_VERIFIED", pages={"PD": [3], "RD": [7, 7]})
        self.assertEqual([s["state"] for s in (no_docs, no_value, not_comparable, error, incomplete, clear)],
                         [card.NO_DOCUMENTS, card.NO_VALUE, card.NOT_COMPARABLE, card.PROCESSING_ERROR, card.INCOMPLETE, card.NO_DIFFERENCE])
        for item in (no_docs, no_value, not_comparable, error, incomplete, clear):
            self.assertTrue(item["reason"] and item["action"]["label"] and item["title"])
        self.assertEqual(no_docs["action"]["kind"], "upload")
        self.assertEqual(no_docs["action"]["stage"], "RD")
        self.assertEqual(not_comparable["action"]["kind"], "revisions")

    def test_missing_stage_names_the_absent_stage_from_the_annotation_tier(self):
        state = self.state("MISSING_EVIDENCE", delta={"reason": "missing_stage", "stages": ["ID"]}, visible_stages=[], has_fragments=False)
        self.assertEqual((state["state"], state["action"]["stage"]), (card.NO_DOCUMENTS, "ID"))
        self.assertIn("ИД", state["action"]["label"])

    def test_no_difference_never_claims_unviewed_pages_conform(self):
        clear = self.state("NEGATIVE_VERIFIED", pages={"PD": [3], "RD": [7]})
        self.assertIn("остальные страницы", clear["reason"])
        self.assertIn("ПД с. 3", clear["reason"])


class CompoundPartsTests(unittest.TestCase):
    def test_set_of_systems_is_split_into_parts(self):
        parts = card.compound_parts({"values": {"PD": "V2.7,V2.8,V2.9", "RD": "MISSING_EXHAUST"}})
        self.assertEqual([p["label"] for p in parts], ["V2.7", "V2.8", "V2.9"])
        self.assertTrue(all(p["differs"] for p in parts))
        mixed = card.compound_parts({"values": {"PD": "V2.10", "RD": "V2.3,V2.4"}})
        self.assertEqual([(p["label"], p["state"]) for p in mixed],
                         [("V2.10", "пропала в РД"), ("V2.3", "добавлена в РД"), ("V2.4", "добавлена в РД")])

    def test_component_wise_comparison_gives_one_part_per_component(self):
        parts = card.compound_parts({"components": [{"label": "ширина", "expected": 600, "actual": 500, "equal": False},
                                                    {"label": "высота", "expected": 400, "actual": 400, "equal": True}]})
        self.assertEqual([(p["label"], p["differs"]) for p in parts], [("ширина", True), ("высота", False)])

    def test_single_values_and_sizes_are_not_compound(self):
        self.assertEqual(card.compound_parts({"values": {"PD": "P15", "RD": "19x19,200x200"}}), [])
        self.assertEqual(card.compound_parts({"values": {"PD": "V2.1", "RD": "V2.1"}}), [])
        self.assertEqual(card.compound_parts({}), [])


class ExplanationTests(unittest.TestCase):
    param = SimpleNamespace(unit="мм", trigger_logic="Уменьшение сечения в РД", sp_reference="СП 60.13330", gost_reference=None, fz_reference=None)

    def test_probability_is_never_claimed_and_missing_fields_stay_none(self):
        block = card.explanation_block(delta={}, param=self.param, expected="Системы: V1", actual="не распознана", confidence=0.72, rule_version="pack-v2")
        self.assertIsNone(block["probability"])
        self.assertIsNone(block["deviation_abs"])
        self.assertIsNone(block["text_confidence"])
        self.assertIsNone(block["link_confidence"])
        self.assertEqual(block["combined_confidence"], 0.72)
        self.assertIn("СП 60.13330", block["threshold_basis"])

    def test_deviation_comes_from_the_trigger_evaluation_else_from_two_plain_numbers(self):
        delta = {"trigger": {"label": "DECREASE", "kind": "DECREASE", "evaluation": "FIRES", "change": "-100", "change_pct": "16.67"},
                 "text_confidence": 0.9, "link_confidence": 0.6}
        block = card.explanation_block(delta=delta, param=self.param, expected="600", actual="500", confidence=0.6, rule_version=None)
        self.assertEqual((block["deviation_abs"], block["deviation_pct"], block["deviation_basis"]), ("-100", "16.67", "правило триггера"))
        self.assertEqual((block["text_confidence"], block["link_confidence"]), (0.9, 0.6))
        plain = card.explanation_block(delta={}, param=self.param, expected="600 мм", actual="500 мм", confidence=None, rule_version=None)
        self.assertEqual((plain["deviation_abs"], plain["deviation_pct"]), (-100.0, 16.67))
        ambiguous = card.explanation_block(delta={}, param=self.param, expected="600x400", actual="500x400", confidence=None, rule_version=None)
        self.assertIsNone(ambiguous["deviation_abs"])


class CardEndpointTests(WorkbenchTestBase):
    def compound_group(self):
        group = self.seed()
        row = self.db.get(EvidenceGroup, group["id"])
        row.delta = {**(row.delta or {}), "values": {"PD": "V2.1,V2.2,V2.3", "RD": "V2.1"}, "location": "140"}
        self.db.commit()
        return group["id"]

    def test_card_payload_carries_every_checklist_block(self):
        group = self.seed()
        data = self.client.get(f"/api/case10/evidence-groups/{group['id']}/workbench").json()
        for key in ("no_result", "parts", "hypothesis", "identity"):
            self.assertIn(key, data)
        explanation = data["card"]["explanation"]
        self.assertIsNone(explanation["probability"])
        self.assertIn("deviation_abs", explanation)
        self.assertIn("sheets", data["card"])
        for item in data["effective_fragments"]:
            self.assertIn("sheet", item)
            self.assertIn("stage_info", item)
        queue = self.client.get(f"/api/case10/processes/{self.process_id}/workbench").json()
        self.assertTrue(all("no_result" in row and "compound_parts" in row for row in queue["items"]))

    def test_partial_decision_per_part_then_overall_decision_when_complete(self):
        gid = self.compound_group()
        data = self.client.get(f"/api/case10/evidence-groups/{gid}/workbench").json()
        self.assertTrue(data["parts"]["compound"])
        self.assertEqual([p["label"] for p in data["parts"]["parts"]], ["V2.1", "V2.2", "V2.3"])
        url = f"/api/case10/evidence-groups/{gid}/partial-decision"

        # a rejected part without a reason is not saved
        refused = self.post(url, {"parts": [{"key": "s:V2.2", "decision": "Reject"}]})
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual(self.db.query(InspectorEdit).filter(InspectorEdit.entity_type == card.ENTITY_PART).count(), 0)

        first = self.post(url, {"parts": [{"key": "s:V2.2", "decision": "Confirm"}, {"key": "s:V2.3", "decision": "Reject", "comment": "система перенесена в другое помещение"}]})
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertIsNone(body["group_decision"])
        self.assertTrue(body["parts"]["partial"])
        self.assertEqual((body["parts"]["decided"], body["parts"]["total"]), (2, 3))
        self.assertEqual(self.db.query(EvidenceDecision).filter(EvidenceDecision.evidence_group_id == gid).count(), 0)
        self.assertEqual(body["evidence_group"]["inspector_status"], "PENDING")

        last = self.post(url, {"parts": [{"key": "s:V2.1", "decision": "Confirm"}], "comment": "проверено на листах ПД и РД"})
        self.assertEqual(last.status_code, 200, last.text)
        done = last.json()
        self.assertEqual(done["group_decision"]["decision"], "Confirm")
        self.assertEqual(done["evidence_group"]["inspector_status"], "CONFIRMED")
        decision = self.db.query(EvidenceDecision).filter(EvidenceDecision.evidence_group_id == gid).one()
        self.assertIn("подтверждено: V2.1, V2.2", decision.comment)
        self.assertIn("отклонено: V2.3", decision.comment)
        # append-only: three new versions, each with author and reason
        edits = self.db.query(InspectorEdit).filter(InspectorEdit.entity_type == card.ENTITY_PART).order_by(InspectorEdit.id).all()
        self.assertEqual(len(edits), 3)
        self.assertTrue(all(e.user_id == self.inspector.id and e.reason and e.version == 1 for e in edits))

    def test_part_decisions_are_rejected_for_a_simple_finding_and_after_finalization(self):
        group = self.seed()
        simple = self.post(f"/api/case10/evidence-groups/{group['id']}/partial-decision", {"parts": [{"key": "c0", "decision": "Confirm"}]})
        self.assertEqual(simple.status_code, 422, simple.text)
        gid = self.compound_group()
        process = self.db.get(InspectionProcess, self.process_id)
        process.status = "FINALIZED"
        self.db.commit()
        blocked = self.post(f"/api/case10/evidence-groups/{gid}/partial-decision", {"parts": [{"key": "s:V2.1", "decision": "Confirm"}]})
        self.assertEqual(blocked.status_code, 409, blocked.text)

    def test_protocol_entry_and_progress(self):
        group = self.seed()
        entry = self.client.get(f"/api/case10/evidence-groups/{group['id']}/protocol-entry").json()
        self.assertEqual(entry["evidence_group_id"], entry["check"]["evidence_group_id"])
        self.assertIn("section", entry)
        self.assertIn("sources", entry)
        self.assertEqual(entry["status"]["finding_status"], "CANDIDATE")
        progress = self.client.get(f"/api/case10/processes/{self.process_id}/progress").json()
        self.assertEqual([s["stage"] for s in progress["stages"]], ["PD", "RD", "ID"])
        self.assertIn("incomplete", progress)
        self.assertGreaterEqual(progress["candidates_found"], 2)
        self.assertEqual(progress["candidates_reviewed"], 0)
        self.assertIsInstance(progress["incomplete_reasons"], list)
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "PART_DECISION_RECORDED").count(), 0)

    def test_hypothesis_card_has_the_tz95_fields(self):
        group = self.seed()
        row = self.db.get(EvidenceGroup, group["id"])
        row.finding_status = "SUSPICION"
        row.delta = {**(row.delta or {}), "matrix_scope": "FREE_SEARCH", "discovery_method": "LOGICAL_ANALYSIS", "rule_name": "Высота ограждения",
                     "description": "Перепад высот без ограждения", "criticality": "HIGH", "normative_base": "СП 1.13130"}
        self.db.commit()
        data = self.client.get(f"/api/case10/evidence-groups/{group['id']}/workbench").json()
        hypothesis = data["hypothesis"]
        self.assertEqual(hypothesis["method"], "LOGICAL_ANALYSIS")
        self.assertEqual(hypothesis["criticality"], "HIGH")
        self.assertEqual(hypothesis["normative_basis"].split(";")[0], "СП 1.13130")
        for key in ("confidence", "description", "pd_reference", "rd_reference", "review_status"):
            self.assertIn(key, hypothesis)
        self.assertIsNone(data["no_result"])

    def make_hypothesis(self):
        group = self.seed()
        row = self.db.get(EvidenceGroup, group["id"])
        row.finding_status = "SUSPICION"
        row.delta = {**(row.delta or {}), "matrix_scope": "FREE_SEARCH", "discovery_method": "LOGICAL_ANALYSIS", "description": "d", "criticality": "HIGH"}
        self.db.commit()
        return group["id"]

    def test_hypothesis_review_has_its_own_status_reason_and_history(self):
        gid = self.make_hypothesis()
        url = f"/api/case10/evidence-groups/{gid}/hypothesis-review"
        self.assertEqual(self.client.get(f"/api/case10/evidence-groups/{gid}/workbench").json()["hypothesis"]["review_status"], "PENDING")

        refused = self.post(url, {"status": "REJECTED", "comment": ""})
        self.assertEqual(refused.status_code, 422, refused.text)
        bad = self.post(url, {"status": "MAYBE", "comment": "не знаю"})
        self.assertEqual(bad.status_code, 422, bad.text)

        first = self.post(url, {"status": "CLARIFICATION_REQUIRED", "comment": "нужен лист ПД с разрезом"})
        self.assertEqual(first.status_code, 200, first.text)
        second = self.post(url, {"status": "REJECTED", "comment": "разрез получен, ограждение есть"})
        body = second.json()
        self.assertEqual((body["status"], body["previous_status"]), ("REJECTED", "CLARIFICATION_REQUIRED"))
        history = body["evidence_group"]["hypothesis"]["review_history"]
        self.assertEqual([h["new_value"]["status"] for h in history], ["CLARIFICATION_REQUIRED", "REJECTED"])
        self.assertTrue(all(h["user_id"] == self.inspector.id and h["reason"] for h in history))
        self.assertEqual(body["evidence_group"]["inspector_status"], "REJECTED")
        # the finding decision log and the finding status are not touched by a hypothesis review
        self.assertEqual(self.db.query(EvidenceDecision).filter(EvidenceDecision.evidence_group_id == gid).count(), 0)
        self.assertEqual(self.db.get(EvidenceGroup, gid).finding_status, "SUSPICION")
        queue = self.client.get(f"/api/case10/processes/{self.process_id}/workbench").json()
        self.assertEqual(next(i for i in queue["items"] if i["id"] == gid)["inspector_status"], "REJECTED")
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "HYPOTHESIS_REVIEWED").count(), 2)

    def test_hypothesis_review_rejects_non_hypotheses_and_finalized_protocols(self):
        group = self.seed()
        plain = self.post(f"/api/case10/evidence-groups/{group['id']}/hypothesis-review", {"status": "CONFIRMED", "comment": "основание"})
        self.assertEqual(plain.status_code, 422, plain.text)
        gid = self.make_hypothesis()
        process = self.db.get(InspectionProcess, self.process_id)
        process.status = "FINALIZED"
        self.db.commit()
        blocked = self.post(f"/api/case10/evidence-groups/{gid}/hypothesis-review", {"status": "CONFIRMED", "comment": "основание"})
        self.assertEqual(blocked.status_code, 409, blocked.text)


if __name__ == "__main__":
    unittest.main()
