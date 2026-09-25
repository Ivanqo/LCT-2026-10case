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
    AuditLog,
    EvidenceDecision,
    EvidenceGroup,
    InspectionProcess,
    Organization,
    Project,
    Protocol,
    User,
)
from app.db.session import Base, get_db


class VerificationLifecycleTests(unittest.TestCase):
    """Covers the CANDIDATE -> verdict -> VERIFICATION_COMPLETED (PROCESS_COMPLETED)
    -> PROTOCOL_FINALIZED state machine end to end: allowed/forbidden finding
    transitions, finalize gating on unresolved CANDIDATEs, post-finalize
    immutability, role-gated unfinalize, and audit/idempotency guarantees."""

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

        org = Organization(name="Lifecycle Test Org", slug="lifecycle-test-org")
        self.db.add(org)
        self.db.flush()

        self.admin = User(
            login="lifecycle-admin", email="admin@lifecycle.test", password_hash="x",
            api_key="admin-key", is_admin=True, role="ADMIN", organization_id=org.id,
        )
        self.inspector = User(
            login="lifecycle-inspector", email="inspector@lifecycle.test", password_hash="x",
            api_key="inspector-key", is_admin=False, role="INSPECTOR", organization_id=org.id,
        )
        self.supervisor = User(
            login="lifecycle-supervisor", email="supervisor@lifecycle.test", password_hash="x",
            api_key="supervisor-key", is_admin=False, role="SUPERVISOR", organization_id=org.id,
        )
        project = Project(name="Lifecycle Project", description="verification lifecycle", organization_id=org.id)
        self.db.add_all([self.admin, self.inspector, self.supervisor, project])
        self.db.commit()
        for user in (self.admin, self.inspector, self.supervisor):
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
        return process_id, groups, by_status

    def _decide(self, group_id: int, decision: str, *, reason_code=None, comment=None):
        return self.client.post(
            f"/api/case10/evidence-groups/{group_id}/decisions",
            json={"decision": decision, "reason_code": reason_code, "comment": comment},
        )

    def _current_protocol(self, project_id, process_id):
        return self.client.get(
            f"/api/case10/protocols/current?project_id={project_id}&process_id={process_id}"
        ).json()

    def _resolve_all_candidates(self, candidates):
        """Confirm every candidate but the last, which is marked
        CLARIFICATION_REQUIRED -- exercising the ТЗ-mandated exception that lets
        finalize proceed with clarifications outstanding but no bare CANDIDATE."""
        for row in candidates[:-1]:
            resp = self._decide(row["id"], "Confirm", comment="resolved for lifecycle test")
            self.assertEqual(resp.status_code, 200, resp.text)
        resp = self._decide(candidates[-1]["id"], "Clarification Required", comment="needs more evidence")
        self.assertEqual(resp.status_code, 200, resp.text)

    # -- allowed / forbidden finding transitions --------------------------

    def test_allowed_finding_decision_transitions_and_redecision(self):
        _, _, by_status = self._seed_process()
        candidates = by_status.get("CANDIDATE", [])
        self.assertGreaterEqual(len(candidates), 3)

        confirm = self._decide(candidates[0]["id"], "Confirm", comment="looks like a violation")
        self.assertEqual(confirm.status_code, 200, confirm.text)
        self.assertEqual(confirm.json()["finding_status"], "CONFIRMED_VIOLATION")

        reject = self._decide(candidates[1]["id"], "Reject", reason_code="OTHER", comment="not a violation")
        self.assertEqual(reject.status_code, 200, reject.text)
        self.assertEqual(reject.json()["finding_status"], "NEGATIVE_VERIFIED")

        clarify = self._decide(candidates[2]["id"], "Clarification Required", comment="need more context")
        self.assertEqual(clarify.status_code, 200, clarify.text)
        self.assertEqual(clarify.json()["finding_status"], "CLARIFICATION_REQUIRED")

        # Re-deciding among the three decidable outcomes is allowed any number
        # of times before finalization.
        redecided = self._decide(candidates[0]["id"], "Reject", reason_code="OTHER", comment="changed my mind")
        self.assertEqual(redecided.status_code, 200, redecided.text)
        self.assertEqual(redecided.json()["finding_status"], "NEGATIVE_VERIFIED")

    def test_decision_on_non_decidable_finding_status_is_rejected(self):
        _, _, by_status = self._seed_process()
        for blocked_status in ("MISSING_EVIDENCE", "NOT_APPLICABLE"):
            rows = by_status.get(blocked_status, [])
            if not rows:
                continue
            group = rows[0]
            response = self._decide(group["id"], "Confirm", comment="should not be allowed")
            self.assertEqual(response.status_code, 409, response.text)
            self.db.expire_all()
            self.assertEqual(self.db.get(EvidenceGroup, group["id"]).finding_status, blocked_status)

    def test_reject_without_reason_or_comment_is_rejected(self):
        _, _, by_status = self._seed_process()
        candidate = by_status["CANDIDATE"][0]
        response = self._decide(candidate["id"], "Reject")
        self.assertEqual(response.status_code, 422, response.text)
        self.db.expire_all()
        self.assertEqual(self.db.get(EvidenceGroup, candidate["id"]).finding_status, "CANDIDATE")

    # -- finalize gating ---------------------------------------------------

    def test_finalize_blocked_while_candidates_pending(self):
        process_id, _, by_status = self._seed_process()
        candidates = by_status["CANDIDATE"]
        self.assertGreaterEqual(len(candidates), 2)

        # Resolve only one of several candidates.
        resp = self._decide(candidates[0]["id"], "Confirm", comment="first one only")
        self.assertEqual(resp.status_code, 200, resp.text)

        protocol = self._current_protocol(self.project.id, process_id)
        finalize = self.client.post(f"/api/case10/protocols/{protocol['id']}/finalize")
        self.assertEqual(finalize.status_code, 409, finalize.text)
        self.assertIn("CANDIDATE", finalize.json()["detail"])

        self.db.expire_all()
        self.assertEqual(self.db.get(Protocol, protocol["id"]).status, "DRAFT")
        self.assertEqual(self.db.get(InspectionProcess, process_id).status, "VERIFYING")

    def test_finalize_succeeds_once_all_candidates_are_resolved(self):
        process_id, _, by_status = self._seed_process()
        candidates = by_status["CANDIDATE"]
        self._resolve_all_candidates(candidates)

        protocol = self._current_protocol(self.project.id, process_id)
        finalize = self.client.post(f"/api/case10/protocols/{protocol['id']}/finalize")
        self.assertEqual(finalize.status_code, 200, finalize.text)
        body = finalize.json()
        self.assertEqual(body["status"], "FINALIZED")
        self.assertEqual(body["finalized_by_user_id"], self.inspector.id)
        self.assertIsNotNone(body["finalized_at"])

        self.db.expire_all()
        process = self.db.get(InspectionProcess, process_id)
        self.assertEqual(process.status, "FINALIZED")
        self.assertEqual(process.finalized_by_user_id, self.inspector.id)

    # -- immutability after finalize ---------------------------------------

    def test_mutations_blocked_after_finalize(self):
        process_id, _, by_status = self._seed_process()
        candidates = by_status["CANDIDATE"]
        self._resolve_all_candidates(candidates)
        protocol = self._current_protocol(self.project.id, process_id)
        finalize = self.client.post(f"/api/case10/protocols/{protocol['id']}/finalize")
        self.assertEqual(finalize.status_code, 200, finalize.text)

        # Decisions are frozen.
        blocked_decision = self._decide(candidates[0]["id"], "Reject", reason_code="OTHER", comment="too late")
        self.assertEqual(blocked_decision.status_code, 409, blocked_decision.text)

        # Silent recomputation of the same process is blocked.
        blocked_run = self.client.post(f"/api/case10/processes/{process_id}/run")
        self.assertEqual(blocked_run.status_code, 409, blocked_run.text)

        # The protocol payload itself is unchanged (still the finalized version).
        after = self._current_protocol(self.project.id, process_id)
        self.assertEqual(after["version"], protocol["version"])
        self.assertEqual(after["status"], "FINALIZED")

    # -- unfinalize RBAC + audit --------------------------------------------

    def _finalize_fully(self):
        process_id, _, by_status = self._seed_process()
        self._resolve_all_candidates(by_status["CANDIDATE"])
        protocol = self._current_protocol(self.project.id, process_id)
        finalize = self.client.post(f"/api/case10/protocols/{protocol['id']}/finalize")
        self.assertEqual(finalize.status_code, 200, finalize.text)
        return process_id, protocol["id"]

    def test_regular_inspector_cannot_unfinalize(self):
        process_id, protocol_id = self._finalize_fully()
        self.current_user = self.inspector
        response = self.client.post(
            f"/api/case10/protocols/{protocol_id}/unfinalize", json={"reason": "trying anyway"}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.db.expire_all()
        self.assertEqual(self.db.get(Protocol, protocol_id).status, "FINALIZED")

    def test_unfinalize_requires_a_reason(self):
        process_id, protocol_id = self._finalize_fully()
        self.current_user = self.supervisor
        response = self.client.post(f"/api/case10/protocols/{protocol_id}/unfinalize", json={"reason": ""})
        self.assertEqual(response.status_code, 422, response.text)

    def test_unfinalize_on_non_finalized_protocol_is_rejected(self):
        process_id, _, by_status = self._seed_process()
        draft_protocol = self._current_protocol(self.project.id, process_id)
        self.current_user = self.supervisor
        response = self.client.post(
            f"/api/case10/protocols/{draft_protocol['id']}/unfinalize", json={"reason": "not finalized yet"}
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_supervisor_can_unfinalize_and_then_editing_resumes(self):
        process_id, protocol_id = self._finalize_fully()

        self.current_user = self.supervisor
        response = self.client.post(
            f"/api/case10/protocols/{protocol_id}/unfinalize",
            json={"reason": "found a data entry error, needs re-review"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "DRAFT")
        self.assertEqual(body["unfinalized_by_user_id"], self.supervisor.id)
        self.assertEqual(body["unfinalize_reason"], "found a data entry error, needs re-review")

        self.db.expire_all()
        process = self.db.get(InspectionProcess, process_id)
        self.assertEqual(process.status, "COMPLETED")
        self.assertIsNone(process.finalized_at)

        # Editing resumes: an admin (also a valid supervisor) can now re-decide
        # a finding that had already been confirmed.
        self.current_user = self.admin
        groups = self.client.get(
            f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={process_id}"
        ).json()
        confirmed = next(row for row in groups if row["finding_status"] == "CONFIRMED_VIOLATION")
        redecided = self._decide(confirmed["id"], "Reject", reason_code="OTHER", comment="re-reviewed, not a violation")
        self.assertEqual(redecided.status_code, 200, redecided.text)

    # -- ML_ENGINEER RBAC on retraining endpoints ---------------------------

    def test_inspector_and_supervisor_are_forbidden_from_ml_retraining_endpoints(self):
        for role_user in (self.inspector, self.supervisor):
            self.current_user = role_user
            log_response = self.client.get(f"/api/case10/projects/{self.project.id}/ml-retraining-log")
            self.assertEqual(log_response.status_code, 403, log_response.text)

            release_response = self.client.post(f"/api/case10/projects/{self.project.id}/training-release")
            self.assertEqual(release_response.status_code, 403, release_response.text)

    def test_ml_engineer_and_admin_can_access_ml_retraining_endpoints(self):
        ml_engineer = User(
            login="lifecycle-ml-engineer", email="ml-engineer@lifecycle.test", password_hash="x",
            api_key="ml-engineer-key", is_admin=False, role="ML_ENGINEER", organization_id=self.project.organization_id,
        )
        self.db.add(ml_engineer)
        self.db.commit()
        self.db.refresh(ml_engineer)

        for role_user in (ml_engineer, self.admin):
            self.current_user = role_user
            log_response = self.client.get(f"/api/case10/projects/{self.project.id}/ml-retraining-log")
            self.assertEqual(log_response.status_code, 200, log_response.text)

            release_response = self.client.post(f"/api/case10/projects/{self.project.id}/training-release")
            self.assertEqual(release_response.status_code, 200, release_response.text)

    # -- audit consistency ----------------------------------------------------

    def test_audit_log_captures_decision_finalize_and_unfinalize(self):
        process_id, protocol_id = self._finalize_fully()
        self.current_user = self.supervisor
        unfinalize = self.client.post(
            f"/api/case10/protocols/{protocol_id}/unfinalize", json={"reason": "undo for audit test"}
        )
        self.assertEqual(unfinalize.status_code, 200, unfinalize.text)

        audit = self.client.get(f"/api/case10/processes/{process_id}/audit").json()
        actions = [row["action"] for row in audit]
        self.assertIn("INSPECTOR_DECISION", actions)
        self.assertIn("PROTOCOL_FINALIZED", actions)
        self.assertIn("PROTOCOL_UNFINALIZED", actions)

        finalize_row = next(row for row in audit if row["action"] == "PROTOCOL_FINALIZED")
        self.assertEqual(finalize_row["user_id"], self.inspector.id)
        self.assertIsNotNone(finalize_row["timestamp"])

        unfinalize_row = next(row for row in audit if row["action"] == "PROTOCOL_UNFINALIZED")
        self.assertEqual(unfinalize_row["user_id"], self.supervisor.id)
        self.assertEqual(unfinalize_row["details"]["reason"], "undo for audit test")
        self.assertIsNotNone(unfinalize_row["timestamp"])

        for decision_row in (row for row in audit if row["action"] == "INSPECTOR_DECISION"):
            self.assertIsNotNone(decision_row["user_id"])
            self.assertIn("finding_status", decision_row["details"])
            self.assertIn("protocol_version", decision_row["details"])

    # -- idempotency and concurrency -------------------------------------------

    def test_repeated_finalize_call_is_idempotent(self):
        process_id, protocol_id = self._finalize_fully()
        first = self.db.get(Protocol, protocol_id)
        first_finalized_at = first.finalized_at

        repeat = self.client.post(f"/api/case10/protocols/{protocol_id}/finalize")
        self.assertEqual(repeat.status_code, 200, repeat.text)
        self.assertEqual(repeat.json()["status"], "FINALIZED")

        self.db.expire_all()
        self.assertEqual(self.db.get(Protocol, protocol_id).finalized_at, first_finalized_at)
        audit = self.client.get(f"/api/case10/processes/{process_id}/audit").json()
        finalize_events = [row for row in audit if row["action"] == "PROTOCOL_FINALIZED"]
        self.assertEqual(len(finalize_events), 1)

    def test_repeated_identical_decision_does_not_corrupt_state(self):
        _, _, by_status = self._seed_process()
        candidate = by_status["CANDIDATE"][0]

        first = self._decide(candidate["id"], "Confirm", comment="verified")
        self.assertEqual(first.status_code, 200, first.text)
        second = self._decide(candidate["id"], "Confirm", comment="verified")
        self.assertEqual(second.status_code, 200, second.text)

        self.db.expire_all()
        self.assertEqual(self.db.get(EvidenceGroup, candidate["id"]).finding_status, "CONFIRMED_VIOLATION")
        decisions = self.db.query(EvidenceDecision).filter(EvidenceDecision.evidence_group_id == candidate["id"]).all()
        self.assertEqual(len(decisions), 2)

    def test_competing_decisions_on_same_finding_last_write_wins_with_full_history(self):
        _, _, by_status = self._seed_process()
        candidate = by_status["CANDIDATE"][0]

        self.current_user = self.inspector
        first = self._decide(candidate["id"], "Confirm", comment="inspector A: violation")
        self.assertEqual(first.status_code, 200, first.text)

        self.current_user = self.supervisor
        second = self._decide(candidate["id"], "Reject", reason_code="OTHER", comment="inspector B: overruled")
        self.assertEqual(second.status_code, 200, second.text)

        self.db.expire_all()
        self.assertEqual(self.db.get(EvidenceGroup, candidate["id"]).finding_status, "NEGATIVE_VERIFIED")
        decisions = (
            self.db.query(EvidenceDecision)
            .filter(EvidenceDecision.evidence_group_id == candidate["id"])
            .order_by(EvidenceDecision.id)
            .all()
        )
        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[0].user_id, self.inspector.id)
        self.assertEqual(decisions[0].decision, "Confirm")
        self.assertEqual(decisions[1].user_id, self.supervisor.id)
        self.assertEqual(decisions[1].decision, "Reject")


if __name__ == "__main__":
    unittest.main()
