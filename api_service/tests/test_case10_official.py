from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.db.models import SourceFragment
from app.domain.official_evidence import inference_annotation
from app.domain.official_dataset import (
    MATRIX_VERSION_OFFICIAL,
    _approval_status_for_row,
    _load_matrix_v11,
    _review_priority_v11,
    _validate_matrix_v11,
    find_dataset_paths,
)
from app.domain.official_rule_packs import (
    _extract_exhaust_systems,
    _extract_supply_configuration,
    _pd_exhaust_room_rows,
    _pd_supply_room_rows,
    parse_rule_page,
    rule_is_violation,
)
from evaluation.exporter import protocol_to_submission, protocol_to_evaluation_predictions, validate_submission_schema
from evaluation.fixtures import LeakageGuardError, leakage_decision, gold_checks_to_evaluation_fixture
from evaluation.metrics import evaluate_case10


class OfficialDataTests(unittest.TestCase):
    @staticmethod
    def _snapshot(text):
        words = []
        cursor = 10.0
        for token in text.split():
            width = max(8.0, len(token) * 4.0)
            words.append({"text": token, "bbox": [cursor, 10.0, cursor + width, 20.0]})
            cursor += width + 3.0
        return {"page": 1, "width": 1000.0, "height": 500.0, "text": text, "words": words}

    @staticmethod
    def _document():
        return SimpleNamespace(id=1, dataset_file_id="F1", filename="source.pdf", file_hash="hash",
                               content_hash="hash", discipline="KR", document_code="DOC", revision="1",
                               approval_status="UNKNOWN", dataset_stage="PD", doc_stage="project")

    def test_gold_annotations_never_become_inference_inputs(self):
        fragment = SourceFragment(source_system="learning_annotation", metadata_json={
            "annotation_type": "MATRIX_FIELD", "status": "AUTO_FIELD_CANDIDATE", "code": "KR-055"})
        self.assertTrue(inference_annotation(fragment))
        fragment.metadata_json = {**fragment.metadata_json, "check_id": "TRAIN-001"}
        self.assertFalse(inference_annotation(fragment))
        fragment.metadata_json = {"annotation_type": "GOLD_EVIDENCE", "status": "FINAL_GOLD_EXISTENCE"}
        self.assertFalse(inference_annotation(fragment))

    def test_missing_approval_is_unknown(self):
        self.assertEqual(_approval_status_for_row({"stage": "PD"}), "UNKNOWN")

    def test_matrix_v11_xlsx_has_official_codes_and_priority_split(self):
        self.assertEqual(MATRIX_VERSION_OFFICIAL, "official-132-v1.1")
        path = find_dataset_paths()["matrix_v11_xlsx"]
        rows = _load_matrix_v11(path)
        _validate_matrix_v11(rows)
        self.assertEqual(len(rows), 132)
        self.assertEqual(rows[78]["matrix_code"], "M-078")
        self.assertEqual(rows[79]["matrix_code"], "M-079")
        self.assertEqual(sum(1 for row in rows.values() if _review_priority_v11(row.get("review_priority")) == "HIGH"), 106)
        self.assertEqual(sum(1 for row in rows.values() if _review_priority_v11(row.get("review_priority")) == "MEDIUM"), 26)

    def test_training_cannot_override_hidden_or_organizer_guard(self):
        for split, visibility in (("TEST_HIDDEN", "PUBLIC_TRAIN_LABEL"), ("TRAIN_PUBLIC", "ORGANIZER_ONLY")):
            decision = leakage_decision({"split": split, "visibility": visibility}, purpose="training",
                                        allow_hidden_labels=True, allow_organizer_only=True)
            self.assertFalse(decision["allowed"])

    def test_gold_replay_cannot_be_exported(self):
        protocol = {"findings": [{"id": 1, "delta": {"source": "gold_fixture"}}]}
        for export in (protocol_to_submission, protocol_to_evaluation_predictions):
            with self.assertRaises(LeakageGuardError):
                export(protocol)

    def test_inspector_reject_overrides_stale_label_and_does_not_invent_id_value(self):
        group = {"id": 1, "object_id": "obj", "finding_status": "NEGATIVE_VERIFIED",
                 "parameter": {"code": "KR-055"}, "actual": "B30",
                 "delta": {"violation_label": "VIOLATION_PRESENT", "protocol_status": "CRITICAL"},
                 "fragments": [{"stage": "working", "file_id": "F1", "page": 1, "extracted_value": "B30"}]}
        result = protocol_to_submission({"object_id": "obj", "findings": [group]})
        check = result["checks"][0]
        self.assertEqual(check["violation_label"], "NO_VIOLATION")
        self.assertEqual(check["protocol_status"], "OK")
        self.assertIsNone(check["id_value"])
        self.assertEqual(validate_submission_schema(result, find_dataset_paths()["submission_schema"]), [])
        check["evidence"][0]["stage"] = "RD_ID_MIXED"
        self.assertTrue(validate_submission_schema(result, find_dataset_paths()["submission_schema"]))

    def test_evaluation_export_includes_rule_comparison_result_and_normalized_values(self):
        group = {"id": 1, "object_id": "obj", "finding_status": "CANDIDATE",
                 "parameter": {"code": "IOS4-078", "review_priority": "HIGH"},
                 "delta": {"parameter_code": "IOS4-078", "location": "314",
                           "comparison_result": "TRIGGERED",
                           "values": {"PD": "V2.12,V2.13", "RD": "MISSING_EXHAUST"}},
                 "fragments": [{"stage": "working", "file_id": "F1", "page": 20, "extracted_value": "MISSING_EXHAUST"}]}
        finding = protocol_to_evaluation_predictions({"object_id": "obj", "findings": [group]})["findings"][0]
        self.assertEqual(finding["comparison_result"], "TRIGGERED")
        self.assertEqual(finding["expected_value"], "V2.12,V2.13")
        self.assertEqual(finding["actual_value"], "MISSING_EXHAUST")
        self.assertEqual(finding["normalized_value"], "PD:V2.12,V2.13 | RD:MISSING_EXHAUST")

    def test_metrics_match_independent_ids_count_extra_positives_and_file_page(self):
        gold = {"total_params": 132,
                "findings": [{"id": "GOLD-1", "object_id": "obj", "parameter_code": "KR-055", "location": "A", "is_violation": True}],
                "evidence": [{"id": "GOLD-1:E1", "finding_id": "GOLD-1", "file_id": "F1", "page": 1}]}
        pred = {"findings": [{"id": "runtime-9", "object_id": "obj", "parameter_code": "KR-055", "location": "A", "status": "CANDIDATE"},
                             {"id": "extra", "status": "CANDIDATE"}],
                "evidence": [{"id": "unrelated", "finding_id": "runtime-9", "file_id": "wrong", "page": 1}]}
        metrics = evaluate_case10(gold, pred)
        self.assertEqual(metrics["finding_precision"], 0.5)
        self.assertEqual(metrics["finding_recall"], 1)
        self.assertEqual(metrics["source_localization_exact_file_page"], 0)
        self.assertIsNone(metrics["evidence_bbox_iou"])
        pred["evidence"][0]["file_id"] = "F1"
        self.assertEqual(evaluate_case10(gold, pred)["evidence_localization_accuracy"], 1)

    def test_free_hypothesis_excluded_from_official_gold(self):
        result = gold_checks_to_evaluation_fixture([{"check_id": "FREE-1", "matrix_scope": "FREE_SEARCH", "violation_label": "VIOLATION_PRESENT"}])
        self.assertEqual(result["findings"], [])

    def test_official_rule_pack_extracts_values_with_source_bbox(self):
        pz = parse_rule_page("PZ-009", self._snapshot(
            "За относительную отметку 0,000 принята абсолютная отметка 159,950 м"
        ), document=self._document(), stage="PD")
        concrete = parse_rule_page("KR-055", self._snapshot(
            "Корпус К1 фундаментная плита B40 пилоны колонны стены В60"
        ), document=self._document(), stage="PD")
        thickness = parse_rule_page("KR-058", self._snapshot(
            "Толщина фундаментной плиты определена расчетом 1000мм 1200мм"
        ), document=self._document(), stage="PD")
        rd_thickness = parse_rule_page("KR-058", self._snapshot(
            "Фундамент дома выполнен в виде монолитной железобетонной плиты толщиной 1200 и 1500 мм"
        ), document=self._document(), stage="RD")
        preparation = parse_rule_page("KR-055", self._snapshot(
            "Под телом фундаментной плиты выполнена бетонная подготовка из бетона B10"
        ), document=self._document(), stage="RD")
        except_foundation = parse_rule_page("KR-055", self._snapshot(
            "Все несущие конструкции нулевого цикла кроме фундаментной плиты выполнены из бетона класса B60"
        ), document=self._document(), stage="RD")
        self.assertEqual(pz[0].normalized_value, "159.95")
        self.assertTrue(all(0 <= value <= 1 for value in pz[0].bbox_normalized))
        self.assertEqual({row.location: row.normalized_value for row in concrete}, {
            "Фундаментная плита": "B40",
            "Вертикальные конструкции подземной части": "B60",
        })
        self.assertEqual(thickness[0].normalized_value, "1000/1200")
        self.assertEqual(rd_thickness[0].normalized_value, "1200/1500")
        self.assertEqual(preparation, [])
        self.assertEqual(except_foundation[0].location, "Вертикальные конструкции подземной части")

    def test_official_trigger_semantics_do_not_turn_non_decrease_into_violation(self):
        self.assertFalse(rule_is_violation("PZ-009", "159.950", "159.95"))
        self.assertFalse(rule_is_violation("KR-055", "B40", "B40"))
        self.assertTrue(rule_is_violation("KR-055", "B40", "B30"))
        self.assertFalse(rule_is_violation("KR-058", "1000/1200", "1200/1500"))
        self.assertTrue(rule_is_violation("KR-058", "1200/1500", "1000/1200"))
        self.assertFalse(rule_is_violation("IOS4-078", "V2.12,V2.13", "V2.11,V2.12,V2.13"))
        self.assertTrue(rule_is_violation("IOS4-078", "V2.7,V2.8,V2.9", "V2.8,V2.9,V2.10"))
        self.assertFalse(rule_is_violation("IOS4-078", "V2.2,V2.3", "V2.9"))
        self.assertTrue(rule_is_violation("IOS4-078", "V2.2,V2.3", "MISSING_EXHAUST"))
        self.assertFalse(rule_is_violation("IOS4-079", "P16", "200x100,350x200,400x350,600x500"))
        self.assertTrue(rule_is_violation("IOS4-079", "P15", "19x19,50x350,200x200,300x100,350x300,400x300"))

    def test_ventilation_ocr_normalization_handles_common_drawing_errors(self):
        self.assertEqual(_extract_exhaust_systems("B2.4 32.10 52.8 82.9"), "V2.4,V2.8,V2.9,V2.10")
        self.assertEqual(
            _extract_supply_configuration("P2.1 P18 P3 P10 P15 1200x350 800X400"),
            "P2.1,P3,P10,P15,P18,800x400,1200x350",
        )

    def test_ventilation_layout_links_room_to_nearby_project_system(self):
        exhaust = {
            "page": 8, "width": 1000.0, "height": 500.0, "text": "",
            "words": [
                {"text": "В2.7", "bbox": [195.0, 112.0, 220.0, 122.0]},
                {"text": "V2.8", "bbox": [195.0, 124.0, 220.0, 134.0]},
                {"text": "140", "bbox": [200.0, 145.0, 218.0, 157.0]},
            ],
        }
        supply = {
            "page": 9, "width": 1000.0, "height": 500.0, "text": "",
            "words": [
                {"text": "P15", "bbox": [175.0, 188.0, 197.0, 199.0]},
                {"text": "012", "bbox": [200.0, 200.0, 218.0, 212.0]},
                {"text": "Венткамера", "bbox": [202.0, 213.0, 270.0, 224.0]},
            ],
        }
        exhaust_rows = _pd_exhaust_room_rows(exhaust)
        supply_rows = _pd_supply_room_rows(supply)
        self.assertEqual(exhaust_rows[0]["normalized_value"], "V2.7,V2.8")
        self.assertEqual(exhaust_rows[0]["location"], "140")
        self.assertEqual(supply_rows[0]["normalized_value"], "P15")
        self.assertEqual(supply_rows[0]["location"], "012")

    def test_exhaust_layout_fallback_links_300_room_only_on_same_row(self):
        exhaust = {
            "page": 8, "width": 1000.0, "height": 500.0, "text": "",
            "words": [
                {"text": "В2.12", "bbox": [80.0, 100.0, 112.0, 112.0]},
                {"text": "В2.13", "bbox": [80.0, 114.0, 112.0, 126.0]},
                {"text": "314", "bbox": [190.0, 112.0, 210.0, 124.0]},
                {"text": "В2.12", "bbox": [420.0, 100.0, 452.0, 112.0]},
                {"text": "304", "bbox": [500.0, 126.0, 520.0, 138.0]},
            ],
        }
        rows = _pd_exhaust_room_rows(exhaust)
        fallback_rows = [row for row in rows if row.get("linkage") == "same_row_fallback"]
        self.assertEqual({row["location"] for row in fallback_rows}, {"314"})
        self.assertEqual(fallback_rows[0]["normalized_value"], "V2.12,V2.13")


if __name__ == "__main__":
    unittest.main()
