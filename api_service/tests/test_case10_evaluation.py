from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evaluation.exporter import evidence_group_to_prediction
from evaluation.fixtures import build_public_metric_fixture, gold_checks_to_evaluation_fixture
from evaluation.metrics import bbox_iou, evaluate_case10


class Case10EvaluationTests(unittest.TestCase):
    def test_official_metrics_are_calculated(self):
        gold = {
            "total_params": 4,
            "ocr": [{"id": "p1", "text": "wall thickness 200 mm"}],
            "key_fields": [
                {"id": "k1", "value": "200 mm"},
                {"id": "k2", "value": "REI 120"},
            ],
            "document_links": [{"id": "d1", "target_document_id": "PD-1"}],
            "evidence": [{"id": "e1", "page": 3, "bbox": [0.1, 0.1, 0.5, 0.5]}],
            "findings": [
                {"id": "f1", "is_violation": True, "category": "AR"},
                {"id": "f2", "is_violation": False, "category": "AR"},
                {"id": "f3", "is_violation": True, "category": "KR"},
                {"id": "f4", "is_violation": False, "category": "KR"},
            ],
        }
        predictions = {
            "ocr": [{"id": "p1", "text": "wall thickness 200 mm"}],
            "key_fields": [
                {"id": "k1", "value": "200 MM"},
                {"id": "k2", "value": "REI 90"},
            ],
            "document_links": [{"id": "d1", "target_document_id": "PD-1"}],
            "evidence": [{"id": "e1", "page": 3, "bbox": [0.1, 0.1, 0.5, 0.5]}],
            "findings": [
                {"id": "f1", "status": "CANDIDATE"},
                {"id": "f2", "status": "NEGATIVE_VERIFIED"},
                {"id": "f3", "status": "MISSING_EVIDENCE"},
                {"id": "f4", "status": "SUSPICION"},
            ],
        }

        result = evaluate_case10(gold, predictions)

        self.assertEqual(result["ocr_character_accuracy"], 1.0)
        self.assertEqual(result["key_field_exact_match"], 0.5)
        self.assertEqual(result["document_linking_accuracy"], 1.0)
        self.assertEqual(result["evidence_page_accuracy"], 1.0)
        self.assertEqual(result["evidence_bbox_iou"], 1.0)
        self.assertEqual(result["evidence_localization_accuracy"], 1.0)
        self.assertEqual(result["finding_precision"], 1.0)
        self.assertEqual(result["finding_recall"], 0.5)
        self.assertAlmostEqual(result["finding_f1"], 2 / 3)
        self.assertEqual(result["false_positive_rate"], 0.0)
        self.assertEqual(result["coverage"], 0.5)
        self.assertEqual(result["abstention_rate"], 0.5)
        self.assertIn("AR", result["per_category_metrics"])

    def test_localization_adjudication_adds_a_source_verified_metric_without_changing_the_strict_one(self):
        # A documented, evidence-backed GOLD localization conflict (checkpoint
        # 30, evaluation/audit_localization.py): the public gold page for one
        # finding is independently shown to be wrong, but the strict metric
        # (gated on acceptance) must still score against public gold as-is.
        gold = {
            "total_params": 2,
            "evidence": [
                {"id": "e1", "finding_id": "CHK-1", "file_id": "F1", "page": 18, "bbox": None},
                {"id": "e2", "finding_id": "CHK-2", "file_id": "F1", "page": 5, "bbox": None},
            ],
            "findings": [{"id": "CHK-1"}, {"id": "CHK-2"}],
        }
        predictions = {
            "evidence": [
                {"id": "pe1", "finding_id": "CHK-1", "file_id": "F1", "page": 20},
                {"id": "pe2", "finding_id": "CHK-2", "file_id": "F1", "page": 5},
            ],
            "findings": [{"id": "CHK-1"}, {"id": "CHK-2"}],
        }
        adjudications = [{
            "check_id": "CHK-1",
            "file_id": "F1",
            "independently_observed_page": 20,
            "reason": "POSSIBLE_GOLD_LOCALIZATION_CONFLICT",
        }]

        strict_only = evaluate_case10(gold, predictions)
        self.assertEqual(strict_only["evidence_localization_accuracy"], 0.5)
        self.assertEqual(strict_only["evidence_localization_accuracy_source_verified"], 0.5)

        adjudicated = evaluate_case10(gold, predictions, localization_adjudications=adjudications)
        # Strict metric is untouched by the adjudication.
        self.assertEqual(adjudicated["evidence_localization_accuracy"], 0.5)
        # Source-verified metric additionally credits the one documented case.
        self.assertEqual(adjudicated["evidence_localization_accuracy_source_verified"], 1.0)
        self.assertEqual(adjudicated["evidence_localization_adjudicated_cases_applied"], 1)

    def test_an_unmatched_adjudication_does_not_change_anything(self):
        gold = {
            "total_params": 1,
            "evidence": [{"id": "e1", "finding_id": "CHK-1", "file_id": "F1", "page": 18, "bbox": None}],
            "findings": [{"id": "CHK-1"}],
        }
        predictions = {
            "evidence": [{"id": "pe1", "finding_id": "CHK-1", "file_id": "F1", "page": 3}],
            "findings": [{"id": "CHK-1"}],
        }
        adjudications = [{
            "check_id": "CHK-1",
            "file_id": "F1",
            "independently_observed_page": 20,
            "reason": "POSSIBLE_GOLD_LOCALIZATION_CONFLICT",
        }]

        result = evaluate_case10(gold, predictions, localization_adjudications=adjudications)
        self.assertEqual(result["evidence_localization_accuracy"], 0.0)
        # Prediction (page 3) matches neither public gold (18) nor the
        # adjudicated source-verified page (20) -- adjudication must not be
        # applied just because a conflict is on file for this finding.
        self.assertEqual(result["evidence_localization_accuracy_source_verified"], 0.0)
        self.assertEqual(result["evidence_localization_adjudicated_cases_applied"], 0)

    def test_bbox_iou_handles_missing_or_out_of_range_boxes(self):
        self.assertEqual(bbox_iou(None, [0, 0, 1, 1]), 0.0)
        self.assertEqual(bbox_iou([0, 0, 1, 1], [0, 0, 1, 1]), 1.0)
        self.assertAlmostEqual(bbox_iou([-1, -1, 0.5, 0.5], [0.25, 0.25, 1.5, 1.5]), 1 / 12)

    def test_status_value_accuracy_uses_canonical_runtime_result(self):
        gold = gold_checks_to_evaluation_fixture([
            {
                "check_id": "GOLD-1",
                "matrix_scope": "MATRIX",
                "object_id": "obj",
                "parameter_code": "IOS4-078",
                "location": "314",
                "violation_label": "VIOLATION_PRESENT",
                "protocol_status": "CRITICAL",
                "comparison_result": "CONFIGURATION_MISMATCH",
            }
        ])
        predictions = {
            "findings": [
                {
                    "id": "runtime-1",
                    "object_id": "obj",
                    "parameter_code": "IOS4-078",
                    "location": "314",
                    "status": "CANDIDATE",
                    "protocol_status": "CRITICAL",
                    "comparison_result": "TRIGGERED",
                }
            ]
        }

        result = evaluate_case10(gold, predictions)

        self.assertEqual(result["finding_precision"], 1.0)
        self.assertEqual(result["finding_recall"], 1.0)
        self.assertEqual(result["normalized_value_and_status_accuracy"], 1.0)
        self.assertEqual(result["status_value_items"], 3)

    def test_public_metric_fixture_keeps_ocr_key_fields_null_with_reasons(self):
        fixture = build_public_metric_fixture(Path("learning_data/extracted/train_public_203/data"))
        predictions = {
            "findings": [],
            "evidence": [],
            "document_links": [],
        }

        result = evaluate_case10(fixture, predictions)

        self.assertEqual(result["counts"]["ocr_items"], 0)
        self.assertEqual(result["counts"]["key_fields"], 0)
        self.assertGreater(result["counts"]["document_links"], 0)
        self.assertIsNone(result["ocr_character_accuracy"])
        self.assertIsNone(result["key_field_exact_match"])
        self.assertIn("No public/train-safe human OCR", result["quality_gates"]["ocr_character_accuracy"]["reason"])
        self.assertGreater(fixture["diagnostics"]["document_field_candidates"], 0)

    def test_small_all_success_sample_yields_wide_confidence_interval(self):
        # ТЗ 14.3: every point metric must publish sample size + a 95% CI.
        # n=6 all-success is exactly the shape of the real Tyumenskaya P0
        # baseline (precision/recall/F1 all 1.0) -- the interval must stay
        # visibly wide, not collapse to [1.0, 1.0], so the report doesn't
        # overstate confidence a 6-item sample can't support.
        gold = {
            "findings": [{"id": f"f{i}", "is_violation": True, "category": "AR"} for i in range(1, 7)],
        }
        predictions = {
            "findings": [{"id": f"f{i}", "status": "CONFIRMED_VIOLATION"} for i in range(1, 7)],
        }

        result = evaluate_case10(gold, predictions)

        self.assertEqual(result["finding_precision"], 1.0)
        self.assertEqual(result["finding_precision_sample_size"], 6)
        low, high = result["finding_precision_confidence_interval_95"]
        self.assertLess(low, 0.9)
        self.assertEqual(high, 1.0)
        self.assertNotEqual([low, high], [1.0, 1.0])

        self.assertEqual(result["finding_recall_sample_size"], 6)
        recall_low, recall_high = result["finding_recall_confidence_interval_95"]
        self.assertLess(recall_low, 0.9)

        # The per-category breakdown (ТЗ 14.3: metrics "по разделам") must
        # carry the same sample-size/CI fields, not just the aggregate.
        category_metrics = result["per_category_metrics"]["AR"]
        self.assertEqual(category_metrics["finding_precision_sample_size"], 6)
        self.assertIsNotNone(category_metrics["finding_precision_confidence_interval_95"])

    def test_zero_sample_size_yields_null_confidence_interval_without_raising(self):
        gold = {"findings": []}
        predictions = {"findings": []}

        result = evaluate_case10(gold, predictions)

        self.assertIsNone(result["finding_precision_sample_size"])
        self.assertIsNone(result["finding_precision_confidence_interval_95"])
        self.assertIsNone(result["finding_recall_sample_size"])
        self.assertIsNone(result["finding_f1_sample_size"])
        self.assertIsNone(result["false_positive_rate_sample_size"])
        self.assertIsNone(result["ocr_character_accuracy_sample_size"])
        self.assertIsNone(result["ocr_character_accuracy_confidence_interval_95"])
        self.assertIsNone(result["key_field_exact_match_sample_size"])
        self.assertIsNone(result["document_linking_accuracy_sample_size"])
        self.assertIsNone(result["evidence_localization_accuracy_sample_size"])

    def test_cli_writes_metrics_json(self):
        gold = {
            "ocr": [{"id": "p1", "text": "abc"}],
            "findings": [{"id": "f1", "is_violation": True}],
        }
        predictions = {
            "ocr": [{"id": "p1", "text": "abc"}],
            "findings": [{"id": "f1", "status": "CONFIRMED_VIOLATION"}],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            gold_path = root / "gold.json"
            predictions_path = root / "predictions.json"
            output_path = root / "metrics.json"
            gold_path.write_text(json.dumps(gold), encoding="utf-8")
            predictions_path.write_text(json.dumps(predictions), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "evaluation.run_evaluation",
                    "--gold",
                    str(gold_path),
                    "--predictions",
                    str(predictions_path),
                    "--output",
                    str(output_path),
                ],
                cwd=Path(__file__).resolve().parents[2],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertTrue(output_path.exists())
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["finding_precision"], 1.0)
            self.assertIn("ocr_character_accuracy", completed.stdout)


class GenericTierLocationExportTests(unittest.TestCase):
    """Regression test for the location-blindness bug shared by all four
    generic (data-driven, not per-code) anchor mechanisms
    (generic_matrix_extraction.py, generic_enum_extraction.py,
    generic_compound_extraction.py, generic_table_row_count.py):
    `_upsert_*_group` in official_evidence.py used to omit `delta["location"]`
    entirely. Since `EvidenceGroup` has no `entity_name` column,
    `evidence_group_to_prediction`'s own
    `delta.get("location", group.get("entity_name"))` fallback always
    resolved to "" for these mechanisms' findings -- so a real, otherwise-
    correct generic-tier finding could never structurally match a
    location-scoped gold check via `metrics._finding_key`
    ((object_id, parameter_code, location)).

    Both states are proven through the real exporter + evaluate_case10()
    pipeline: the frozen pre-fix delta shape (test 1) never aligns; the
    fixed `_upsert_generic_group` output (test 2 -- representative of all
    four mechanisms, which share this exact fix via
    `anchor_search.GENERIC_SITE_LOCATION`) does."""

    def _gold_fixture(self, location: str) -> dict:
        return gold_checks_to_evaluation_fixture([{
            "check_id": "GOLD-LOC-1",
            "matrix_scope": "MATRIX",
            "object_id": "OBJ-SYNTHETIC-LOC",
            "parameter_code": "PZ-301",
            "location": location,
            "violation_label": "VIOLATION_PRESENT",
            "protocol_status": "CRITICAL",
            "comparison_result": "VALUE_MISMATCH",
        }])

    def test_pre_fix_delta_shape_never_aligns_with_a_location_scoped_gold_check(self):
        # Frozen snapshot of the OLD `_upsert_generic_group` delta (no
        # "location" key at all) -- reproduces the bug's exact failure
        # signature directly, without depending on the now-fixed production
        # code, so this half of the regression stays meaningful even as the
        # fix evolves.
        group = {
            "id": "grp-1",
            "object_id": "OBJ-SYNTHETIC-LOC",
            "finding_status": "CANDIDATE",
            "delta": {
                "source": "generic_anchor_extractor",
                "matrix_scope": "MATRIX",
                "parameter_code": "PZ-301",
                "comparison_result": "TRIGGERED",
                # no "location" key -- this was the bug.
            },
            "fragments": [],
        }
        prediction = evidence_group_to_prediction(group)
        self.assertEqual(prediction["finding"]["location"], "")

        gold = self._gold_fixture("SITE")
        result = evaluate_case10(gold, {"findings": [prediction["finding"]]})
        # Structurally can never align: gold requires a non-empty, matching
        # location and the pre-fix export always produced "".
        self.assertEqual(result["finding_recall"], 0.0)

    def test_fixed_group_carries_a_matching_location_and_aligns_with_gold(self):
        from app.db.models import EvidenceGroup
        from app.db.session import Base
        from app.domain.anchor_search import GENERIC_SITE_LOCATION
        from app.domain.generic_matrix_extraction import GenericObservation
        from app.domain.official_evidence import _upsert_generic_group

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        process = SimpleNamespace(
            id="proc-loc", project_id=1, organization_id=1, object_id="OBJ-SYNTHETIC-LOC",
            matrix_version="official-132-v1", dataset_version="synthetic-v1", upload_scenario="FULL",
        )
        param = SimpleNamespace(
            id=301, code="PZ-301", review_priority="HIGH", source_pd=True, source_rd=True, source_id=False,
        )

        def observation(value: str, *, doc_id: int) -> GenericObservation:
            return GenericObservation(
                value=value, normalized_value=value, decimal_value=Decimal(value), confidence=0.55,
                document=SimpleNamespace(
                    id=doc_id, dataset_file_id=f"SYN-{doc_id}", file_hash="hash", content_hash="hash",
                    discipline="PZ", document_code="DOC", revision="1", approval_status="UNKNOWN",
                ),
                page=1, bbox_normalized=[0.1, 0.1, 0.2, 0.12], bbox_pdf=[10.0, 500.0, 40.0, 510.0],
                page_width=1000.0, page_height=800.0, extractor="generic_anchor_numeric",
                context="synthetic context", source_fragment=None,
            )

        try:
            _upsert_generic_group(db, process, param, {
                "PD": observation("100", doc_id=1),
                "RD": observation("90", doc_id=2),
            })
            group_row = db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC-LOC").one()
            self.assertEqual(group_row.delta.get("location"), GENERIC_SITE_LOCATION)

            group = {
                "id": str(group_row.id),
                "object_id": group_row.object_id,
                "finding_status": group_row.finding_status,
                "delta": group_row.delta,
                "fragments": [],
            }
            prediction = evidence_group_to_prediction(group)
            self.assertEqual(prediction["finding"]["location"], GENERIC_SITE_LOCATION)

            gold = self._gold_fixture(GENERIC_SITE_LOCATION)
            result = evaluate_case10(gold, {"findings": [prediction["finding"]]})
            self.assertEqual(result["finding_recall"], 1.0)
        finally:
            db.close()
