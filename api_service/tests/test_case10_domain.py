from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import AttributeObservation, ChangeIssue, Organization, Project
from app.db.session import Base
from app.domain.change_engine import CHANGE_VALUE_CHANGED, ChangeEngine
from app.domain.entity_resolution import EntityResolver, ObservationDraft
from app.domain.normalization import normalize_alias
from app.domain.portrait_builder import PortraitBuilder
from app.domain.synthetic_dataset import ensure_synthetic_case10_dataset


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


if __name__ == "__main__":
    unittest.main()

