from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import AttributeObservation, ChangeIssue, Organization, Project
from app.db.session import Base
from app.domain.change_engine import CHANGE_VALUE_CHANGED, ChangeEngine
from app.domain.entity_resolution import EntityResolver, ObservationDraft
from app.domain.normalization import normalize_alias
from app.domain.portrait_builder import PortraitBuilder
from app.domain.synthetic_dataset import ensure_synthetic_case10_dataset
from app.domain.v3_pipeline import document_completeness, upload_scenario
from app.domain.protocol_annex2 import annex2_payload


class Case10DomainTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()

        org = Organization(name="Test Org", slug="test-org")
        self.db.add(org)
        self.db.flush()
        project = Project(name="Synthetic Project", description="CASE10 tests", organization_id=org.id)
        self.db.add(project)
        self.db.commit()
        self.org_id = org.id
        self.project_id = project.id

    def tearDown(self):
        self.db.close()

    def test_alias_normalization(self):
        self.assertEqual(normalize_alias(" СТ-01 "), "ст01")
        self.assertEqual(normalize_alias("Wall A-1"), "walla1")

    def test_synthetic_dataset_builds_portrait(self):
        result = ensure_synthetic_case10_dataset(self.db, self.project_id, self.org_id)
        self.assertTrue(result["created"])

        portrait = PortraitBuilder().build(self.db, result["canonical_entity_id"], self.org_id)

        self.assertEqual(portrait["identity"]["entity_type"], "wall")
        self.assertIn("project", portrait["stages"])
        self.assertIn("working", portrait["stages"])
        self.assertIn("as_built", portrait["stages"])
        self.assertIn("thickness", portrait["attributes"])
        self.assertEqual(len(portrait["attributes"]["thickness"]), 3)
        self.assertTrue(any(rel["relation_type"] == "LOCATED_IN" for rel in portrait["relations"]))

    def test_entity_resolver_returns_explainable_candidate(self):
        result = ensure_synthetic_case10_dataset(self.db, self.project_id, self.org_id)
        draft = ObservationDraft(
            project_id=self.project_id,
            organization_id=self.org_id,
            raw_name="Wall ST-01",
            raw_mark="ST-01",
            entity_type="wall",
            location={"building": "1", "floor": "3", "axes": "A-B / 4-7"},
            attributes={"thickness": 180, "fire_resistance": "EI30", "material": "Brick"},
        )

        candidate = EntityResolver().best_match(self.db, draft)

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.canonical_entity_id, result["canonical_entity_id"])
        self.assertGreaterEqual(candidate.confidence, 0.4)
        self.assertTrue(candidate.reasons)

    def test_change_engine_detects_value_changes_and_issues(self):
        result = ensure_synthetic_case10_dataset(self.db, self.project_id, self.org_id)

        changes = ChangeEngine().compute_for_entity(self.db, result["canonical_entity_id"])
        persisted_issues = self.db.query(ChangeIssue).all()

        self.assertTrue(any(change.change_type == CHANGE_VALUE_CHANGED for change in changes))
        self.assertTrue(any(change.attribute == "fire_resistance" for change in changes))
        self.assertTrue(any(issue.severity == "CRITICAL" for issue in persisted_issues))
        self.assertGreaterEqual(self.db.query(AttributeObservation).count(), 9)

    def test_registry_ratio_marks_partial_status_in_api_and_protocol_shape(self):
        summary = [
            {"doc_stage": "PD", "file_id": "PD-1", "file_name": "a.pdf"},
            {"doc_stage": "PD", "file_id": "PD-2", "file_name": "b.pdf"},
        ]
        document = SimpleNamespace(
            doc_stage="project", document_stage="project", dataset_stage="PD", dataset_split="BATCH_PACKAGE",
            dataset_file_id="PD-1", dataset_metadata={"document_manifest": {
                "registry_manifest_summary": summary,
                "registry": {"file_id": "PD-1", "doc_stage": "PD", "file_name": "a.pdf"},
            }},
        )
        unmatched = SimpleNamespace(
            doc_stage="project", document_stage="project", dataset_stage="PD", dataset_split="BATCH_PACKAGE",
            dataset_file_id="extra", dataset_metadata={"document_manifest": {"registry_manifest_summary": summary}},
        )
        second_package = SimpleNamespace(
            doc_stage="project", document_stage="project", dataset_stage="PD", dataset_split="BATCH_PACKAGE",
            dataset_file_id="PD-3", dataset_metadata={"document_manifest": {
                "registry_manifest_summary": [{"doc_stage": "PD", "file_id": "PD-3", "file_name": "c.pdf"}],
                "registry": {"file_id": "PD-3", "doc_stage": "PD", "file_name": "c.pdf"},
            }},
        )
        documents = [document, unmatched, second_package]
        completeness = document_completeness(documents, registry_aware=True)
        self.assertEqual(completeness, {"PD_UPLOADED": "PD_PARTIAL", "RD_UPLOADED": "RD_MISSING", "ID_UPLOADED": "ID_MISSING"})
        self.assertEqual(upload_scenario(completeness), "SINGLE_ONLY")
        legacy = document_completeness(documents, registry_aware=False)
        self.assertEqual(legacy, {"PD_UPLOADED": "PD_UPLOADED", "RD_UPLOADED": "RD_MISSING", "ID_UPLOADED": "ID_MISSING"})
        annex = annex2_payload([], documents, object_info={"object_name": "x"}, matrix_size=132,
                               completeness=completeness, include_registry_counts=True)
        pd_upload = next(row for row in annex["section_1_document_upload"]["rows"] if row["stage"] == "PD")
        self.assertEqual((pd_upload["status"], pd_upload["loaded_files"], pd_upload["expected_files"]), ("PD_PARTIAL", 2, 3))

    def test_duplicate_registry_file_ids_do_not_hide_a_missing_record(self):
        summary = [
            {"doc_stage": "PD", "file_id": "DUP", "file_name": "a.pdf", "sha256": "a" * 64},
            {"doc_stage": "PD", "file_id": "DUP", "file_name": "b.pdf", "sha256": "b" * 64},
        ]
        loaded = SimpleNamespace(
            doc_stage="project", dataset_stage="PD", dataset_split="BATCH_PACKAGE",
            dataset_metadata={"document_manifest": {
                "registry_manifest_summary": summary,
                "registry": {"file_id": "DUP", "doc_stage": "PD", "file_name": "a.pdf", "sha256": "a" * 64},
            }},
        )
        completeness = document_completeness([loaded], registry_aware=True)
        self.assertEqual(completeness["PD_UPLOADED"], "PD_PARTIAL")

if __name__ == "__main__":
    unittest.main()

