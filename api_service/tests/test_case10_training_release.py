from __future__ import annotations

import inspect
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import (
    DocumentVersion,
    EvidenceDecision,
    EvidenceFragment,
    EvidenceGroup,
    GoldDraftItem,
    InspectionProcess,
    Organization,
    Project,
)
from app.db.session import Base
from app.domain import official_rule_packs, v3_pipeline
from app.domain.training_release import build_training_release, model_acceptance_gate
from evaluation.fixtures import load_gold_checks_jsonl


class Case10TrainingReleaseTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        org = Organization(name="Training Org", slug="training-org")
        project = Project(name="Training Project", organization_id=1)
        self.db.add(org)
        self.db.flush()
        project.organization_id = org.id
        self.db.add(project)
        self.db.flush()
        self.org = org
        self.project = project
        self.process = InspectionProcess(
            id="training-process",
            project_id=project.id,
            organization_id=org.id,
            object_id="OBJ-TRAIN-A",
            status="READY",
            matrix_version="official-132-v1.1",
            dataset_version="case10-official-public-v1",
            model_version="official-rule-packs-v2",
        )
        self.db.add(self.process)
        self.db.flush()
        self.doc = DocumentVersion(
            project_id=project.id,
            organization_id=org.id,
            source_type="dataset",
            source_document_id=1,
            object_id="OBJ-TRAIN-A",
            dataset_file_id="F1",
            filename="source.pdf",
            document_stage="project",
            doc_stage="project",
            file_hash="hash",
        )
        self.db.add(self.doc)
        self.db.flush()

    def tearDown(self):
        self.db.close()

    def test_training_release_blocks_and_excludes_unfinished_or_hidden_items(self):
        self._draft("CONFIRMED_VIOLATION", "OBJ-TRAIN-A", "KR-055")
        self._draft("NEGATIVE_VERIFIED", "OBJ-TRAIN-B", "KR-058")
        self._draft("CANDIDATE", "OBJ-TRAIN-C", "PZ-009")
        self._draft("CONFIRMED_VIOLATION", "OBJ-TRAIN-HIDDEN", "SPZU-027", split="TEST_HIDDEN")

        result = build_training_release(
            self.db,
            project_id=self.project.id,
            organization_id=self.org.id,
            min_confirmed_violations=100,
            candidate_metrics={"finding_f1": 1.0, "finding_recall": 1.0, "false_positive_rate": 0.0},
            baseline_metrics={"model_version": "baseline", "finding_f1": 1.0, "finding_recall": 1.0, "false_positive_rate": 0.0},
        )

        labels = {item["label"] for item in result["release"]["items"]}
        self.assertEqual(labels, {"CONFIRMED_VIOLATION", "NEGATIVE_VERIFIED"})
        self.assertEqual(result["gate_status"], "BLOCKED")
        self.assertEqual(result["approval_status"], "BLOCKED")
        self.assertTrue(result["no_auto_publish"])
        self.assertIn("confirmed_violations_below_minimum:1<100", result["gate_reasons"])
        self.assertEqual(result["excluded_counts"]["forbidden_label:CANDIDATE"], 1)
        self.assertEqual(result["excluded_counts"]["hidden_or_organizer_only"], 1)

    def test_model_gate_blocks_degradation_and_requests_rollback(self):
        result = model_acceptance_gate(
            {"finding_f1": 0.80, "finding_recall": 0.90, "false_positive_rate": 0.13},
            {"model_version": "official-rule-packs-v2", "finding_f1": 0.90, "finding_recall": 0.94, "false_positive_rate": 0.10},
        )

        self.assertFalse(result["passed"])
        self.assertIn("f1_degradation_gt_5pp", result["reasons"])
        self.assertIn("recall_degradation_gt_2pp", result["reasons"])
        self.assertIn("fpr_growth_gt_2pp", result["reasons"])
        self.assertEqual(result["rollback_to_model_version"], "official-rule-packs-v2")

    def test_model_gate_blocks_on_single_category_regression_even_with_flat_aggregate(self):
        # ТЗ 9.4: no regression "по любой обязательной категории" -- an
        # aggregate that looks unchanged can still hide one category's
        # recall collapsing while another category compensates upward.
        candidate_metrics = {
            "finding_f1": 0.90,
            "finding_recall": 0.90,
            "false_positive_rate": 0.05,
            "per_category_metrics": {
                "AR": {"finding_recall": 0.60, "finding_false_positive_rate": 0.05},
                "KR": {"finding_recall": 1.00, "finding_false_positive_rate": 0.05},
            },
        }
        baseline_metrics = {
            "model_version": "official-rule-packs-v2",
            "finding_f1": 0.90,
            "finding_recall": 0.90,
            "false_positive_rate": 0.05,
            "per_category_metrics": {
                "AR": {"finding_recall": 0.90, "finding_false_positive_rate": 0.05},
                "KR": {"finding_recall": 0.90, "finding_false_positive_rate": 0.05},
            },
        }

        result = model_acceptance_gate(candidate_metrics, baseline_metrics)

        self.assertFalse(result["passed"])
        self.assertIn("recall_degradation_gt_2pp:category=AR", result["reasons"])
        # Aggregate recall did not move -- confirms this is caught only by
        # the per-category check, not the pre-existing aggregate one.
        self.assertNotIn("recall_degradation_gt_2pp", result["reasons"])
        self.assertEqual(result["rollback_to_model_version"], "official-rule-packs-v2")

    def test_model_gate_passes_when_no_category_regresses(self):
        candidate_metrics = {
            "finding_f1": 0.95,
            "finding_recall": 0.95,
            "false_positive_rate": 0.02,
            "per_category_metrics": {
                "AR": {"finding_recall": 0.95, "finding_false_positive_rate": 0.02},
            },
        }
        baseline_metrics = {
            "model_version": "official-rule-packs-v2",
            "finding_f1": 0.90,
            "finding_recall": 0.90,
            "false_positive_rate": 0.05,
            "per_category_metrics": {
                "AR": {"finding_recall": 0.90, "finding_false_positive_rate": 0.05},
            },
        }

        result = model_acceptance_gate(candidate_metrics, baseline_metrics)

        self.assertTrue(result["passed"])
        self.assertEqual(result["reasons"], [])
        self.assertIsNone(result["rollback_to_model_version"])

    def test_hidden_labels_are_blocked_and_runtime_process_does_not_use_gold_fixture_groups(self):
        hidden = load_gold_checks_jsonl(
            "learning_data/extracted/test_hidden_213/data/hidden_gold_checks_organizer_only.jsonl",
            purpose="training",
        )
        self.assertEqual(hidden["summary"]["allowed"], 0)
        self.assertEqual(hidden["summary"]["blocked"], 4)
        self.assertNotIn("_create_gold_fixture_groups", inspect.getsource(v3_pipeline.run_process))
        self.assertNotIn("OBJ-TYUMENSKAYA", inspect.getsource(official_rule_packs))
        self.assertNotIn("OBJ-NOVOSLOBODSKAYA", inspect.getsource(official_rule_packs))
        self.assertNotIn("OBJ-RECHNIKOV", inspect.getsource(official_rule_packs))

    def _draft(self, label: str, object_id: str, code: str, *, split: str | None = None) -> None:
        group = EvidenceGroup(
            process_id=self.process.id,
            project_id=self.project.id,
            organization_id=self.org.id,
            object_id=object_id,
            matrix_version="official-132-v1.1",
            model_version="official-rule-packs-v2",
            dataset_version="case10-official-public-v1",
            comparison_scenario="FULL",
            completeness_status="COMPLETE",
            comparability_status="COMPARABLE",
            finding_status=label,
            delta={"source": "official_rule_pack", "matrix_scope": "MATRIX", "parameter_code": code, "location": "SITE"},
        )
        self.db.add(group)
        self.db.flush()
        fragment = EvidenceFragment(
            evidence_group_id=group.id,
            document_version_id=self.doc.id,
            dataset_file_id="F1",
            file_sha256="hash",
            stage="project",
            page=1,
            bbox=[0.1, 0.1, 0.2, 0.2],
            role="expected",
        )
        decision = EvidenceDecision(evidence_group_id=group.id, decision="Confirm" if label == "CONFIRMED_VIOLATION" else "Reject")
        self.db.add_all([fragment, decision])
        self.db.flush()
        payload = {
            "id": group.id,
            "object_id": object_id,
            "finding_status": label,
            "matrix_version": group.matrix_version,
            "model_version": group.model_version,
            "dataset_version": group.dataset_version,
            "parameter": {"code": code},
            "fragments": [
                {"file_id": "F1", "stage": "project", "page": 1, "bbox_normalized": [0.1, 0.1, 0.2, 0.2], "role": "expected"}
            ],
        }
        if split:
            payload["split"] = split
        self.db.add(
            GoldDraftItem(
                evidence_group_id=group.id,
                decision_id=decision.id,
                project_id=self.project.id,
                organization_id=self.org.id,
                object_id=object_id,
                label=label,
                payload_json=payload,
            )
        )
        self.db.flush()


if __name__ == "__main__":
    unittest.main()
