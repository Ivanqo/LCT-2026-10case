from __future__ import annotations

from pathlib import Path
from time import perf_counter
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.api.auth import get_current_user
from app.api.routes_case10 import router as case10_router
from app.api.routes_upload import router as upload_router
from app.db.models import (
    AttributeObservation,
    Case10ProcessJob,
    DocumentVersion,
    EntityObservation,
    EvidenceDecision,
    EvidenceGroup,
    InspectionProcess,
    Organization,
    Project,
    Protocol,
    SourceFragment,
    User,
)
from app.db.session import Base, get_db
from app.domain.v3_jobs import JOB_PROCESSING, JOB_QUEUED, enqueue_process_job, execute_process_job, recover_queued_jobs
from app.domain.v3_pipeline import MATRIX_VERSION_DEMO


class Case10ApiTests(unittest.TestCase):
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

        org = Organization(name="API Test Org", slug="api-test-org")
        self.db.add(org)
        self.db.flush()
        user = User(
            login="case10",
            email="case10@example.test",
            password_hash="test",
            api_key="test-key",
            is_admin=True,
            organization_id=org.id,
        )
        project = Project(name="CASE10 API Project", description="integration test", organization_id=org.id)
        self.db.add_all([user, project])
        self.db.commit()
        self.db.refresh(user)
        self.db.refresh(project)
        self.user = user
        self.project = project

        app = FastAPI()
        app.include_router(case10_router, prefix="/api")
        app.include_router(upload_router, prefix="/api")

        def override_db():
            try:
                yield self.db
            finally:
                pass

        def override_user():
            return self.user

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        settings.TEMP_UPLOADS_DIR = self._old_temp_uploads_dir
        self._tmp_uploads.cleanup()

    def _execute_latest_case10_job(self, process_id: str, *, redelivered: bool = False):
        self.db.expire_all()
        job = (
            self.db.query(Case10ProcessJob)
            .filter(Case10ProcessJob.process_id == str(process_id))
            .order_by(Case10ProcessJob.created_at.desc(), Case10ProcessJob.id.desc())
            .first()
        )
        self.assertIsNotNone(job)
        payload = dict(job.payload_json or {})
        payload.update(
            {
                "job_id": str(job.id),
                "process_id": str(job.process_id),
                "project_id": int(job.project_id),
                "organization_id": int(job.organization_id),
                "attempt": int(job.attempt_count or 0) + 1,
                "max_attempts": int(job.max_attempts or 3),
            }
        )
        return execute_process_job(self.db, payload, redelivered=redelivered), job

    def test_case10_synthetic_http_flow(self):
        seeded = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        self.assertEqual(seeded.status_code, 200, seeded.text)
        payload = seeded.json()
        self.assertTrue(payload["created"])
        entity_id = payload["canonical_entity_id"]

        overview = self.client.get(f"/api/case10/overview?project_id={self.project.id}")
        self.assertEqual(overview.status_code, 200, overview.text)
        overview_payload = overview.json()
        self.assertEqual(overview_payload["objects"]["total"], 2)
        self.assertGreaterEqual(overview_payload["documents"]["total"], 3)
        self.assertGreaterEqual(overview_payload["changes"]["by_severity"]["CRITICAL"], 1)

        documents = self.client.get(f"/api/case10/documents?project_id={self.project.id}")
        self.assertEqual(documents.status_code, 200, documents.text)
        stages = {group["stage"] for group in documents.json()["groups"]}
        self.assertTrue({"project", "working", "as_built"}.issubset(stages))

        entities = self.client.get(f"/api/entities?project_id={self.project.id}")
        self.assertEqual(entities.status_code, 200, entities.text)
        entity_rows = entities.json()
        self.assertTrue(any(row["id"] == entity_id and row["changes_count"] >= 1 for row in entity_rows))

        portrait = self.client.get(f"/api/entities/{entity_id}/portrait")
        self.assertEqual(portrait.status_code, 200, portrait.text)
        portrait_payload = portrait.json()
        self.assertEqual(portrait_payload["identity"]["id"], entity_id)
        self.assertIn("thickness", portrait_payload["attributes"])
        self.assertGreaterEqual(len(portrait_payload["timeline"]), 3)

        changes = self.client.get(f"/api/case10/changes?project_id={self.project.id}")
        self.assertEqual(changes.status_code, 200, changes.text)
        change_rows = changes.json()
        critical = next(row for row in change_rows if row["risk"]["severity"] == "CRITICAL")
        self.assertEqual(critical["parameter"], "fire_resistance")

        protocol = self.client.get(f"/api/case10/protocol?project_id={self.project.id}")
        self.assertEqual(protocol.status_code, 200, protocol.text)
        issue_id = protocol.json()[0]["issue_id"]

        decision = self.client.post(f"/api/issues/{issue_id}/decisions", json={"decision": "Confirmed"})
        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertEqual(decision.json()["decision"], "Confirmed")

        updated_protocol = self.client.get(f"/api/case10/protocol?project_id={self.project.id}")
        self.assertEqual(updated_protocol.status_code, 200, updated_protocol.text)
        self.assertTrue(any(row["issue_id"] == issue_id and row["protocol_status"] == "Confirmed" for row in updated_protocol.json()))

    def test_synthetic_dataset_can_be_seeded_in_multiple_projects(self):
        second_project = Project(name="Second CASE10 API Project", description="second integration test", organization_id=self.user.organization_id)
        self.db.add(second_project)
        self.db.commit()
        self.db.refresh(second_project)

        first_seeded = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        self.assertEqual(first_seeded.status_code, 200, first_seeded.text)
        second_seeded = self.client.post(f"/api/case10/projects/{second_project.id}/synthetic-dataset")
        self.assertEqual(second_seeded.status_code, 200, second_seeded.text)

        first_entity_id = first_seeded.json()["canonical_entity_id"]
        second_entity_id = second_seeded.json()["canonical_entity_id"]
        self.assertNotEqual(first_entity_id, second_entity_id)
        self.assertIn(f"P{self.project.id:06d}", first_entity_id)
        self.assertIn(f"P{second_project.id:06d}", second_entity_id)

    def test_v3_evidence_protocol_inspector_flow(self):
        seeded = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        self.assertEqual(seeded.status_code, 200, seeded.text)
        process_id = seeded.json()["process_id"]

        status = self.client.get(f"/api/case10/processes/{process_id}/status")
        self.assertEqual(status.status_code, 200, status.text)
        status_payload = status.json()
        self.assertEqual(status_payload["status"], "READY")
        self.assertEqual(status_payload["upload_scenario"], "FULL")
        self.assertGreaterEqual(status_payload["finding_counts"]["CANDIDATE"], 1)

        groups = self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={process_id}")
        self.assertEqual(groups.status_code, 200, groups.text)
        group_rows = groups.json()
        candidate = next(row for row in group_rows if row["finding_status"] == "CANDIDATE")
        self.assertTrue(candidate["expected"])
        self.assertTrue(candidate["actual"])
        self.assertTrue(candidate["fragments"])
        for fragment in candidate["fragments"]:
            self.assertIsNotNone(fragment["page"])
            bbox = fragment["bbox"]
            self.assertIsInstance(bbox, list)
            self.assertEqual(len(bbox), 4)
            self.assertTrue(all(0 <= float(value) <= 1 for value in bbox))

        protocol_before = self.client.get(f"/api/case10/protocols/current?project_id={self.project.id}&process_id={process_id}")
        self.assertEqual(protocol_before.status_code, 200, protocol_before.text)
        version_before = protocol_before.json()["version"]

        decision = self.client.post(
            f"/api/case10/evidence-groups/{candidate['id']}/decisions",
            json={"decision": "Confirm", "comment": "verified in synthetic fixture"},
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertEqual(decision.json()["finding_status"], "CONFIRMED_VIOLATION")

        protocol_after = self.client.get(f"/api/case10/protocols/current?project_id={self.project.id}&process_id={process_id}")
        self.assertEqual(protocol_after.status_code, 200, protocol_after.text)
        latest_protocol = protocol_after.json()
        self.assertEqual(latest_protocol["version"], version_before + 1)
        self.assertTrue(
            any(row["id"] == candidate["id"] and row["finding_status"] == "CONFIRMED_VIOLATION" for row in latest_protocol["payload"]["findings"])
        )

        # Other CANDIDATE findings from the same run are still unresolved: finalizing
        # now must be blocked until each one is either decided or explicitly marked
        # CLARIFICATION_REQUIRED.
        other_candidates = [row for row in group_rows if row["finding_status"] == "CANDIDATE" and row["id"] != candidate["id"]]
        self.assertGreater(len(other_candidates), 0)
        premature_finalize = self.client.post(f"/api/case10/protocols/{latest_protocol['id']}/finalize")
        self.assertEqual(premature_finalize.status_code, 409, premature_finalize.text)
        self.assertIn("CANDIDATE", premature_finalize.json()["detail"])

        for other in other_candidates[:-1]:
            resolved = self.client.post(
                f"/api/case10/evidence-groups/{other['id']}/decisions",
                json={"decision": "Reject", "reason_code": "OTHER", "comment": "resolved in test"},
            )
            self.assertEqual(resolved.status_code, 200, resolved.text)
        clarified = self.client.post(
            f"/api/case10/evidence-groups/{other_candidates[-1]['id']}/decisions",
            json={"decision": "Clarification Required", "comment": "needs more evidence"},
        )
        self.assertEqual(clarified.status_code, 200, clarified.text)
        self.assertEqual(clarified.json()["finding_status"], "CLARIFICATION_REQUIRED")

        latest_protocol = self.client.get(
            f"/api/case10/protocols/current?project_id={self.project.id}&process_id={process_id}"
        ).json()
        finalized = self.client.post(f"/api/case10/protocols/{latest_protocol['id']}/finalize")
        self.assertEqual(finalized.status_code, 200, finalized.text)
        self.assertEqual(finalized.json()["status"], "FINALIZED")

        blocked = self.client.post(
            f"/api/case10/evidence-groups/{candidate['id']}/decisions",
            json={"decision": "Reject", "reason_code": "OTHER", "comment": "should be locked"},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)

    def test_xml_upload_creates_document_version_fragment_and_process_id(self):
        uploaded = self.client.post(
            "/api/upload",
            data={"project_id": str(self.project.id), "file_type": "xml"},
            files={"file": ("approved_project_rev1.xml", b"<root>building area = 100 m2</root>", "application/xml")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        upload_payload = uploaded.json()
        self.assertTrue(upload_payload["process_id"])

        with patch("app.api.routes_upload.SessionLocal", self.Session):
            processed = self.client.post(f"/api/upload-jobs/{upload_payload['upload_id']}/process?project_id={self.project.id}")
        self.assertEqual(processed.status_code, 200, processed.text)

        doc = self.db.query(DocumentVersion).filter(DocumentVersion.source_type == "xml").first()
        self.assertIsNotNone(doc)
        self.assertEqual(doc.document_stage, "project")
        self.assertEqual(doc.approval_status, "APPROVED")
        self.assertTrue(doc.file_hash)

        fragment = self.db.query(SourceFragment).filter(SourceFragment.document_version_id == doc.id).first()
        self.assertIsNotNone(fragment)
        self.assertEqual(fragment.source_system, "xml")
        self.assertEqual(fragment.bbox, [0.0, 0.0, 1.0, 1.0])

        status = self.client.get(f"/api/case10/processes/{upload_payload['process_id']}/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "QUEUED")
        result, _job = self._execute_latest_case10_job(upload_payload["process_id"])
        self.assertEqual(result.outcome, "ready", result)

        status = self.client.get(f"/api/case10/processes/{upload_payload['process_id']}/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "READY")

    def test_start_endpoint_returns_quickly_and_only_enqueues_worker_job(self):
        with patch("app.api.routes_case10.publish_job_message", return_value=True) as published:
            with patch("app.api.routes_case10.run_process") as route_run_process:
                started = perf_counter()
                response = self.client.post(
                    f"/api/case10/projects/{self.project.id}/processes",
                    json={"matrix_version": MATRIX_VERSION_DEMO},
                )
                elapsed = perf_counter() - started
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertLess(elapsed, 1.0)
        self.assertEqual(payload["status"], "QUEUED")
        self.assertTrue(payload["process_id"])
        self.assertEqual(payload["job"]["status"], "QUEUED")
        self.assertEqual(self.db.query(Case10ProcessJob).filter(Case10ProcessJob.process_id == payload["process_id"]).count(), 1)
        self.assertEqual(self.db.query(EvidenceGroup).filter(EvidenceGroup.process_id == payload["process_id"]).count(), 0)
        published.assert_called_once()
        route_run_process.assert_not_called()

    def test_worker_consumes_job_and_marks_process_ready(self):
        seeded = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        self.assertEqual(seeded.status_code, 200, seeded.text)
        with patch("app.api.routes_case10.publish_job_message", return_value=True):
            response = self.client.post(
                f"/api/case10/projects/{self.project.id}/processes",
                json={"matrix_version": MATRIX_VERSION_DEMO},
            )
        self.assertEqual(response.status_code, 200, response.text)
        process_id = response.json()["process_id"]

        result, job = self._execute_latest_case10_job(process_id)
        self.assertEqual(result.outcome, "ready", result)
        self.db.expire_all()
        process = self.db.get(InspectionProcess, process_id)
        self.assertEqual(process.status, "READY")
        self.assertEqual(self.db.get(Case10ProcessJob, job.id).status, "READY")
        self.assertGreater(self.db.query(EvidenceGroup).filter(EvidenceGroup.process_id == process_id).count(), 0)
        self.assertGreater(self.db.query(Protocol).filter(Protocol.process_id == process_id).count(), 0)

    def test_duplicate_delivery_does_not_duplicate_findings_or_reset_decisions(self):
        self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        with patch("app.api.routes_case10.publish_job_message", return_value=True):
            response = self.client.post(
                f"/api/case10/projects/{self.project.id}/processes",
                json={"matrix_version": MATRIX_VERSION_DEMO},
            )
        process_id = response.json()["process_id"]
        result, job = self._execute_latest_case10_job(process_id)
        self.assertEqual(result.outcome, "ready", result)

        groups = self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={process_id}").json()
        candidate = next(row for row in groups if row["finding_status"] == "CANDIDATE")
        decision = self.client.post(
            f"/api/case10/evidence-groups/{candidate['id']}/decisions",
            json={"decision": "Confirm", "comment": "duplicate delivery must not reset this"},
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        before_groups = self.db.query(EvidenceGroup).filter(EvidenceGroup.process_id == process_id).count()
        before_protocols = self.db.query(Protocol).filter(Protocol.process_id == process_id).count()
        before_decisions = self.db.query(EvidenceDecision).count()

        payload = dict(job.payload_json or {})
        payload.update({"job_id": str(job.id), "process_id": process_id, "attempt": int(job.attempt_count or 0) + 1})
        duplicate = execute_process_job(self.db, payload, redelivered=True)
        self.assertEqual(duplicate.outcome, "skipped", duplicate)
        self.assertEqual(self.db.query(EvidenceGroup).filter(EvidenceGroup.process_id == process_id).count(), before_groups)
        self.assertEqual(self.db.query(Protocol).filter(Protocol.process_id == process_id).count(), before_protocols)
        self.assertEqual(self.db.query(EvidenceDecision).count(), before_decisions)
        self.db.expire_all()
        group = self.db.get(EvidenceGroup, candidate["id"])
        self.assertEqual(group.finding_status, "CONFIRMED_VIOLATION")

    def test_retryable_and_permanent_worker_failures(self):
        process = self._new_demo_process()
        job = enqueue_process_job(self.db, process, user_id=self.user.id, reason="test_retry")
        self.db.commit()
        payload = dict(job.payload_json or {})
        payload.update({"job_id": str(job.id), "process_id": str(process.id), "attempt": 1})

        with patch("app.domain.v3_jobs.run_process", side_effect=RuntimeError("temporary outage")):
            retry = execute_process_job(self.db, payload)
        self.assertEqual(retry.outcome, "retry", retry)
        self.db.expire_all()
        self.assertEqual(self.db.get(Case10ProcessJob, job.id).status, "QUEUED")
        self.assertEqual(self.db.get(InspectionProcess, process.id).status, "QUEUED")

        permanent_process = self._new_demo_process()
        permanent_job = enqueue_process_job(self.db, permanent_process, user_id=self.user.id, reason="test_permanent")
        self.db.commit()
        permanent_payload = dict(permanent_job.payload_json or {})
        permanent_payload.update({"job_id": str(permanent_job.id), "process_id": str(permanent_process.id), "attempt": 1})
        with patch("app.domain.v3_jobs.run_process", side_effect=HTTPException(status_code=409, detail="finalized")):
            failed = execute_process_job(self.db, permanent_payload)
        self.assertEqual(failed.outcome, "failed", failed)
        self.db.expire_all()
        self.assertEqual(self.db.get(Case10ProcessJob, permanent_job.id).status, "FAILED")
        self.assertEqual(self.db.get(InspectionProcess, permanent_process.id).status, "FAILED")

    def test_recovery_scan_requeues_saved_jobs_after_restart(self):
        process = self._new_demo_process()
        job = enqueue_process_job(self.db, process, user_id=self.user.id, reason="test_recovery")
        self.db.commit()
        recovered = recover_queued_jobs(self.db, publish=False)
        self.assertTrue(any(row["job_id"] == job.id and row["status"] == "QUEUED" for row in recovered))

        job.status = JOB_PROCESSING
        process.status = "PROCESSING"
        self.db.add_all([job, process])
        self.db.commit()
        recovered_processing = recover_queued_jobs(self.db, publish=False)
        self.assertTrue(any(row["job_id"] == job.id and row["status"] == "QUEUED" for row in recovered_processing))
        self.db.expire_all()
        self.assertEqual(self.db.get(Case10ProcessJob, job.id).status, JOB_QUEUED)

    def test_status_endpoint_only_reads_database_state(self):
        process = self._new_demo_process()
        self.db.commit()
        with patch("app.api.routes_case10.run_process") as route_run_process:
            status = self.client.get(f"/api/case10/processes/{process.id}/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "PENDING")
        route_run_process.assert_not_called()

    def _new_demo_process(self):
        response = self.client.post(
            f"/api/case10/projects/{self.project.id}/processes",
            json={"run_immediately": False, "matrix_version": MATRIX_VERSION_DEMO},
        )
        self.assertEqual(response.status_code, 200, response.text)
        process_id = response.json()["process_id"]
        process = self.db.get(InspectionProcess, process_id)
        self.assertIsNotNone(process)
        return process

    def test_protocol_annex_history_audit_and_stale_finalize(self):
        seeded = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset").json()
        process_id = seeded["process_id"]
        groups = self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={process_id}").json()
        candidates = [row for row in groups if row["finding_status"] == "CANDIDATE"]
        self.assertGreaterEqual(len(candidates), 3)
        candidate = candidates[0]
        other_candidates = candidates[1:]
        current_url = f"/api/case10/protocols/current?project_id={self.project.id}&process_id={process_id}"
        before = self.client.get(current_url).json()
        rejected = self.client.post(f"/api/case10/evidence-groups/{candidate['id']}/decisions", json={"decision": "Reject"})
        self.assertEqual(rejected.status_code, 422)
        self.client.post(f"/api/case10/evidence-groups/{candidate['id']}/decisions", json={"decision": "Confirm", "comment": "Inspector test"})

        # Finalizing must stay blocked while sibling CANDIDATE findings from the
        # same run are still unresolved.
        blocked = self.client.post(f"/api/case10/protocols/{self.client.get(current_url).json()['id']}/finalize")
        self.assertEqual(blocked.status_code, 409, blocked.text)

        for other in other_candidates[:-1]:
            resolved = self.client.post(
                f"/api/case10/evidence-groups/{other['id']}/decisions",
                json={"decision": "Reject", "reason_code": "OTHER", "comment": "resolved in test"},
            )
            self.assertEqual(resolved.status_code, 200, resolved.text)
        self.client.post(
            f"/api/case10/evidence-groups/{other_candidates[-1]['id']}/decisions",
            json={"decision": "Clarification Required", "comment": "needs more evidence"},
        )

        latest = self.client.get(current_url).json()
        self.assertEqual(latest["payload"]["version"], before["version"] + len(candidates))
        annex = latest["payload"]["annex_2"]
        self.assertEqual(len([key for key in annex if key.startswith("section_")]), 7)
        self.assertEqual(len(annex["section_4_critical_violations"]), 1)
        item = annex["section_4_critical_violations"][0]
        self.assertEqual(item["inspector_status"], "CONFIRMED")
        self.assertEqual(item["decisions"][0]["user_id"], self.user.id)
        pdf_response = self.client.get(f"/api/case10/protocols/{latest['id']}/pdf")
        self.assertEqual(pdf_response.status_code, 200, pdf_response.text)
        import fitz
        with fitz.open(stream=pdf_response.content, filetype="pdf") as pdf:
            extracted = "\n".join(page.get_text() for page in pdf)
            self.assertIn("ПРОТОКОЛ АВТОМАТИЗИРОВАННОЙ СВЕРКИ", extracted)
            self.assertIn("РАЗДЕЛ 7. РЕЗОЛЮТИВНАЯ ЧАСТЬ", extracted)
            self.assertGreaterEqual(len(pdf), 7)
        history = self.client.get(f"/api/case10/processes/{process_id}/protocols").json()
        self.assertEqual(len(history), 1 + len(candidates))
        self.assertEqual(history[0]["payload"]["annex_2"]["section_4_critical_violations"], [])
        self.assertEqual(self.client.post(f"/api/case10/protocols/{before['id']}/finalize").status_code, 409)
        final = self.client.post(f"/api/case10/protocols/{latest['id']}/finalize").json()
        self.assertEqual(final["payload"]["verification_status"], "PROTOCOL_FINALIZED")
        self.assertEqual(self.client.post(f"/api/case10/processes/{process_id}/run").status_code, 409)
        audit = self.client.get(f"/api/case10/processes/{process_id}/audit").json()
        decision = next(row for row in audit if row["action"] == "INSPECTOR_DECISION")
        self.assertEqual(decision["details"]["protocol_version"], before["version"] + 1)

    def test_rag_source_fragment_sync_endpoint_is_idempotent(self):
        doc_version = DocumentVersion(
            project_id=self.project.id,
            organization_id=self.user.organization_id,
            source_type="rag",
            source_document_id=77,
            filename="synthetic_rag.pdf",
            document_stage="project",
            discipline="AR",
            version="v1",
        )
        self.db.add(doc_version)
        self.db.commit()
        self.db.refresh(doc_version)

        exported = [
            {
                "source_system": "rag",
                "external_id": "chunk:101",
                "fragment_type": "text",
                "page": 3,
                "bbox": [10, 20, 110, 120],
                "text": "Wall A thickness 250 mm",
                "extractor": "rag_chunk",
                "confidence": 0.75,
                "metadata": {"rag_chunk_id": 101, "rag_page_id": 11},
            }
        ]
        expected_org_id = self.user.organization_id

        class FakeRagClient:
            def __init__(self, *args, **kwargs):
                pass

            async def export_source_fragments(self, *, document_id: int, organization_id: int | None):
                if document_id != 77 or organization_id != expected_org_id:
                    raise AssertionError("Unexpected RAG export request")
                return exported

        with patch("app.api.routes_case10.RagClient", FakeRagClient):
            first = self.client.post(f"/api/case10/document-versions/{doc_version.id}/sync-source-fragments")
            second = self.client.post(f"/api/case10/document-versions/{doc_version.id}/sync-source-fragments")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["created"], 1)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["created"], 0)
        self.assertEqual(second.json()["updated"], 1)
        self.assertEqual(self.db.query(SourceFragment).count(), 1)

        fragment = self.db.query(SourceFragment).first()
        self.assertEqual(fragment.source_system, "rag")
        self.assertEqual(fragment.external_id, "chunk:101")
        self.assertEqual(fragment.page, 3)
        self.assertEqual(fragment.bbox, [10, 20, 110, 120])

    def test_ifc_observation_sync_endpoint_is_idempotent(self):
        doc_version = DocumentVersion(
            project_id=self.project.id,
            organization_id=self.user.organization_id,
            source_type="ifc",
            source_document_id=88,
            filename="synthetic.ifc",
            document_stage="ifc",
            discipline="BIM",
            version="v1",
        )
        self.db.add(doc_version)
        self.db.commit()
        self.db.refresh(doc_version)

        exported = [
            {
                "source_system": "ifc",
                "external_id": "element:501",
                "guid": "2abcGUID",
                "ifc_type": "IfcWall",
                "name": "Wall ST-01",
                "storey_guid": "STOREY-3",
                "container_guid": "CONTAINER-1",
                "attributes": {"Pset_WallCommon.FireRating": "EI60"},
                "quantities": {"Qto_WallBaseQuantities.Width": {"value": 200.0, "unit": "mm"}},
                "metrics": {"area": 12.5, "volume": 3.0, "source": "ifcopenshell"},
                "text": "IFC IfcWall GUID 2abcGUID name Wall ST-01",
            }
        ]
        expected_org_id = self.user.organization_id

        class FakeIfcClient:
            def __init__(self, *args, **kwargs):
                pass

            async def export_observations(self, *, model_id: int, organization_id: int | None, limit: int = 500):
                if model_id != 88 or organization_id != expected_org_id or limit != 500:
                    raise AssertionError("Unexpected IFC export request")
                return exported

        with patch("app.api.routes_case10.IfcClient", FakeIfcClient):
            first = self.client.post(f"/api/case10/document-versions/{doc_version.id}/sync-ifc-observations")
            second = self.client.post(f"/api/case10/document-versions/{doc_version.id}/sync-ifc-observations")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["created_entities"], 1)
        self.assertEqual(first.json()["observations_created"], 1)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["observations_created"], 0)
        self.assertEqual(second.json()["observations_updated"], 1)
        self.assertEqual(self.db.query(SourceFragment).filter(SourceFragment.source_system == "ifc").count(), 1)
        self.assertEqual(self.db.query(EntityObservation).count(), 1)
        self.assertGreaterEqual(self.db.query(AttributeObservation).count(), 3)


if __name__ == "__main__":
    unittest.main()
