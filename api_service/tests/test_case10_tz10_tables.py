"""ТЗ 10 ("Таблицы базы данных (сводка)"): the DB tables that had no
counterpart before this change -- Rejection_Log, Dispute_Log, Logical_Rules
(CRUD surface), Normative_Base, Monitoring_Metrics. `Suspicions` wiring is
covered separately in test_case10_logical_analysis.py, since it is produced
by that module, not by any endpoint here.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.api.auth import get_current_user
from app.api.routes_case10 import router as case10_router
from app.db.models import (
    DisputeLog,
    EvidenceGroup,
    LogicalRuleRecord,
    MonitoringMetric,
    NormativeBaseEntry,
    Organization,
    Project,
    RejectionLog,
    User,
)
from app.db.session import Base, get_db
from app.domain.logical_analysis import LOGICAL_RULES


class Case10Tz10TablesTests(unittest.TestCase):
    def setUp(self):
        self._tmp_uploads = tempfile.TemporaryDirectory()
        self._old_temp_uploads_dir = settings.TEMP_UPLOADS_DIR
        settings.TEMP_UPLOADS_DIR = Path(self._tmp_uploads.name)

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()

        org = Organization(name="TZ10 Test Org", slug="tz10-test-org")
        self.db.add(org)
        self.db.flush()

        self.admin = User(
            login="tz10-admin", email="admin@tz10.test", password_hash="x",
            api_key="admin-key", is_admin=True, role="ADMIN", organization_id=org.id,
        )
        self.inspector = User(
            login="tz10-inspector", email="inspector@tz10.test", password_hash="x",
            api_key="inspector-key", is_admin=False, role="INSPECTOR", organization_id=org.id,
        )
        project = Project(name="TZ10 Project", description="tz10 tables", organization_id=org.id)
        self.db.add_all([self.admin, self.inspector, project])
        self.db.commit()
        for user in (self.admin, self.inspector):
            self.db.refresh(user)
        self.db.refresh(project)
        self.project = project
        self.current_user = self.inspector

        app = FastAPI()
        app.include_router(case10_router, prefix="/api")

        def override_db():
            try:
                yield self.db
            finally:
                pass

        def override_user():
            return self.current_user

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        settings.TEMP_UPLOADS_DIR = self._old_temp_uploads_dir
        self._tmp_uploads.cleanup()

    # -- helpers ---------------------------------------------------------

    def _seed_process(self):
        seeded = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        self.assertEqual(seeded.status_code, 200, seeded.text)
        process_id = seeded.json()["process_id"]
        groups = self.client.get(
            f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={process_id}"
        ).json()
        by_status: dict[str, list[dict]] = {}
        for row in groups:
            by_status.setdefault(row["finding_status"], []).append(row)
        return process_id, by_status

    def _decide(self, group_id: int, decision: str, *, reason_code=None, comment=None):
        return self.client.post(
            f"/api/case10/evidence-groups/{group_id}/decisions",
            json={"decision": decision, "reason_code": reason_code, "comment": comment},
        )

    def _as(self, user: User):
        self.current_user = user
        return self

    # -- Rejection_Log: written on a real Reject decision --------------------

    def test_rejection_log_is_written_when_a_finding_is_rejected(self):
        _, by_status = self._seed_process()
        candidate = by_status["CANDIDATE"][0]
        response = self._decide(candidate["id"], "Reject", reason_code="OCR_ERROR", comment="misread digit")
        self.assertEqual(response.status_code, 200, response.text)

        rows = self.db.query(RejectionLog).filter_by(evidence_group_id=candidate["id"]).all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.rejection_reason, "misread digit")
        self.assertEqual(row.retraining_status, "PENDING")
        self.assertIsNone(row.ai_verdict)  # Module 9.4 is not implemented -- honestly NULL, not fabricated
        self.assertIsNotNone(row.evidence_decision_id)
        self.assertEqual(row.project_id, self.project.id)

    def test_rejection_log_falls_back_to_reason_code_when_comment_is_blank(self):
        _, by_status = self._seed_process()
        candidate = by_status["CANDIDATE"][0]
        response = self._decide(candidate["id"], "Reject", reason_code="NORM_NOT_APPLICABLE")
        self.assertEqual(response.status_code, 200, response.text)
        row = self.db.query(RejectionLog).filter_by(evidence_group_id=candidate["id"]).one()
        self.assertEqual(row.rejection_reason, "NORM_NOT_APPLICABLE")

    def test_confirming_a_finding_does_not_write_a_rejection_log(self):
        _, by_status = self._seed_process()
        candidate = by_status["CANDIDATE"][0]
        response = self._decide(candidate["id"], "Confirm", comment="looks right")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.query(RejectionLog).filter_by(evidence_group_id=candidate["id"]).count(), 0)

    def test_re_rejecting_the_same_finding_writes_a_second_rejection_log_row(self):
        _, by_status = self._seed_process()
        candidate = by_status["CANDIDATE"][0]
        self._decide(candidate["id"], "Confirm", comment="first pass")
        response = self._decide(candidate["id"], "Reject", reason_code="OTHER", comment="changed my mind")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.query(RejectionLog).filter_by(evidence_group_id=candidate["id"]).count(), 1)

    # -- Dispute_Log: create endpoint ----------------------------------------

    def test_create_dispute_log_entry(self):
        _, by_status = self._seed_process()
        candidate = by_status["CANDIDATE"][0]
        response = self.client.post(
            f"/api/case10/evidence-groups/{candidate['id']}/disputes",
            json={"inspector_comment": "I disagree with this candidate finding"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["resolution_status"], "OPEN")
        self.assertEqual(body["opened_by_user_id"], self.inspector.id)
        self.assertIsNone(body["ai_comment"])

        row = self.db.get(DisputeLog, body["id"])
        self.assertIsNotNone(row)
        self.assertEqual(row.evidence_group_id, candidate["id"])
        self.assertEqual(row.organization_id, self.project.organization_id)

    def test_dispute_log_requires_a_comment(self):
        _, by_status = self._seed_process()
        candidate = by_status["CANDIDATE"][0]
        response = self.client.post(f"/api/case10/evidence-groups/{candidate['id']}/disputes", json={"inspector_comment": ""})
        self.assertEqual(response.status_code, 422, response.text)

    def test_dispute_log_404s_for_unknown_evidence_group(self):
        response = self.client.post("/api/case10/evidence-groups/999999/disputes", json={"inspector_comment": "x"})
        self.assertEqual(response.status_code, 404, response.text)

    # -- Logical_Rules: seeded + admin CRUD ----------------------------------

    def _seed_logical_rules_table(self):
        for rule in LOGICAL_RULES:
            self.db.add(LogicalRuleRecord(
                rule_id=rule.rule_id, rule_name=rule.rule_name,
                condition=rule.condition_description, expected=rule.implied_description,
                normative_base=rule.normative_base, criticality=rule.criticality,
                confidence=rule.confidence, is_active=rule.is_active,
            ))
        self.db.commit()

    def test_list_logical_rules_matches_the_code_level_table(self):
        self._seed_logical_rules_table()
        response = self.client.get("/api/case10/logical-rules")
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertEqual({r["rule_id"] for r in rows}, {r.rule_id for r in LOGICAL_RULES})
        self.assertTrue(all(r["is_active"] for r in rows))

    def test_admin_can_deactivate_a_logical_rule(self):
        self._seed_logical_rules_table()
        response = self._as(self.admin).client.patch("/api/case10/logical-rules/LR-001", json={"is_active": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["is_active"])
        self.db.expire_all()
        self.assertFalse(self.db.query(LogicalRuleRecord).filter_by(rule_id="LR-001").one().is_active)

    def test_non_admin_cannot_update_a_logical_rule(self):
        self._seed_logical_rules_table()
        response = self._as(self.inspector).client.patch("/api/case10/logical-rules/LR-001", json={"is_active": False})
        self.assertEqual(response.status_code, 403, response.text)

    def test_update_unknown_logical_rule_404s(self):
        response = self._as(self.admin).client.patch("/api/case10/logical-rules/LR-999", json={"is_active": False})
        self.assertEqual(response.status_code, 404, response.text)

    # -- Normative_Base: full admin CRUD -------------------------------------

    def test_normative_base_create_read_update_delete(self):
        created = self._as(self.admin).client.post(
            "/api/case10/normative-base",
            json={
                "document_name": "СП 1.13130.2020",
                "document_number": "1.13130.2020",
                "section": "4.2.1",
                "parameter_name": "AR-41",
                "min_value": 0.9,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        entry = created.json()
        self.assertEqual(entry["min_value"], 0.9)
        self.assertTrue(entry["is_active"])
        entry_id = entry["id"]

        listed = self.client.get("/api/case10/normative-base?parameter_name=AR-41").json()
        self.assertEqual([row["id"] for row in listed], [entry_id])

        # Also exercises the datetime fields end to end (effective_from is a
        # real DB DateTime column, and lands in the AuditLog.details JSON
        # column too -- both must round-trip through the same PATCH).
        updated = self._as(self.admin).client.patch(
            f"/api/case10/normative-base/{entry_id}",
            json={"min_value": 1.0, "is_active": False, "effective_from": "2026-01-01T00:00:00"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["min_value"], 1.0)
        self.assertFalse(updated.json()["is_active"])
        self.assertTrue(str(updated.json()["effective_from"]).startswith("2026-01-01"))

        still_active_only = self.client.get("/api/case10/normative-base?is_active=true").json()
        self.assertNotIn(entry_id, [row["id"] for row in still_active_only])

        deleted = self._as(self.admin).client.delete(f"/api/case10/normative-base/{entry_id}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertIsNone(self.db.get(NormativeBaseEntry, entry_id))

    def test_normative_base_write_endpoints_require_admin(self):
        response = self._as(self.inspector).client.post(
            "/api/case10/normative-base", json={"document_name": "СП X"}
        )
        self.assertEqual(response.status_code, 403, response.text)

    # -- Monitoring_Metrics: durable, queryable ------------------------------

    def test_monitoring_metrics_are_listed_for_ml_engineer(self):
        self.db.add(MonitoringMetric(metric_name="iais_rin_sync_duration_seconds", value=0.12, service_name="iais_rin_sync", tags={"outcome": "success"}))
        self.db.add(MonitoringMetric(metric_name="unrelated_metric", value=1.0, service_name="other"))
        self.db.commit()

        ml_engineer = User(
            login="tz10-ml", email="ml@tz10.test", password_hash="x",
            api_key="ml-key", is_admin=False, role="ML_ENGINEER", organization_id=self.project.organization_id,
        )
        self.db.add(ml_engineer)
        self.db.commit()
        self.db.refresh(ml_engineer)

        response = self._as(ml_engineer).client.get("/api/case10/monitoring-metrics?metric_name=iais_rin_sync_duration_seconds")
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], {"outcome": "success"})

    def test_monitoring_metrics_are_forbidden_for_a_plain_inspector(self):
        response = self._as(self.inspector).client.get("/api/case10/monitoring-metrics")
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
