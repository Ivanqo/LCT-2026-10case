from __future__ import annotations

from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.api.auth import get_current_user
from app.api import routes_upload
from app.api.routes_upload import router as upload_router
from app.db.models import (
    AuditLog,
    DocumentVersion,
    EvidenceGroup,
    InspectionProcess,
    MatrixVersion,
    Organization,
    Param,
    Project,
    User,
)
from app.db.session import Base, get_db
from app.domain import file_registry
from app.domain import official_evidence
from app.domain import inspector_workbench
from evaluation.exporter import protocol_to_submission


class S3RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._settings = {
            "OFFLINE_DELIVERY": settings.OFFLINE_DELIVERY,
            "AUTO_PROCESS_UPLOADS": settings.AUTO_PROCESS_UPLOADS,
            "RABBITMQ_URL": settings.RABBITMQ_URL,
            "BATCH_PACKAGES_ROOT": settings.BATCH_PACKAGES_ROOT,
            "DATA_DIR": settings.DATA_DIR,
            "TEMP_UPLOADS_DIR": settings.TEMP_UPLOADS_DIR,
        }
        settings.OFFLINE_DELIVERY = True
        settings.AUTO_PROCESS_UPLOADS = True
        settings.RABBITMQ_URL = ""
        settings.BATCH_PACKAGES_ROOT = self.root / "packages"
        settings.DATA_DIR = self.root / "data"
        settings.TEMP_UPLOADS_DIR = self.root / "temp"
        self._originals_root = os.environ.get("CASE10_ORIGINALS_ROOT")
        os.environ.pop("CASE10_ORIGINALS_ROOT", None)
        self._route_session_factory = routes_upload.SessionLocal
        self._route_publish_job = routes_upload.publish_job_message

        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.engine = engine
        self.db = sessionmaker(bind=engine)()
        self.org = Organization(name="S3 Registry Org", slug="s3-registry-org")
        self.project = Project(name="S3 Registry Project", organization_id=None)
        self.db.add(self.org)
        self.db.flush()
        self.project.organization_id = self.org.id
        self.user = User(login="s3-inspector", password_hash="x", api_key="s3-key", role="INSPECTOR",
                         organization_id=self.org.id)
        self.db.add_all([self.project, self.user])
        self.db.flush()
        self.process = InspectionProcess(
            id="s3-process", project_id=self.project.id, organization_id=self.org.id, object_id="S3-OBJECT",
            matrix_version="s3-test-matrix", dataset_version="s3-test-data", status="PENDING",
        )
        matrix = MatrixVersion(version="s3-test-matrix", organization_id=self.org.id, project_id=self.project.id)
        self.db.add_all([self.process, matrix])
        self.db.flush()
        self.matrix = matrix
        self.db.commit()
        self.project_id = int(self.project.id)
        self.organization_id = int(self.org.id)
        self.user_id = int(self.user.id)
        routes_upload.SessionLocal = lambda: self.db
        routes_upload.publish_job_message = lambda _job: None

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        routes_upload.SessionLocal = self._route_session_factory
        routes_upload.publish_job_message = self._route_publish_job
        for key, value in self._settings.items():
            setattr(settings, key, value)
        if self._originals_root is None:
            os.environ.pop("CASE10_ORIGINALS_ROOT", None)
        else:
            os.environ["CASE10_ORIGINALS_ROOT"] = self._originals_root
        self.tmp.cleanup()


class FileRegistryPolicyTests(S3RegistryTestBase):
    def add_document(self, *, name: str, file_id: str, approval: str, stage: str = "PD", predecessor=None,
                     successor=None, registry_status="ACCEPTED", relative=None):
        doc = DocumentVersion(
            project_id=self.project.id,
            organization_id=self.org.id,
            source_type="case10_dataset",
            object_id=self.process.object_id,
            dataset_file_id=file_id,
            dataset_split="BATCH_PACKAGE",
            dataset_stage=stage,
            dataset_section="АР",
            filename=name,
            doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}.get(stage, "unknown"),
            document_stage={"PD": "project", "RD": "working", "ID": "as_built"}.get(stage, "unknown"),
            document_code=name,
            revision="1",
            approval_status=approval,
            approval_date=datetime(2026, 9, 1),
            predecessor_id=predecessor,
            successor_id=successor,
            content_hash=file_id.lower().ljust(64, "0")[:64],
            file_hash=file_id.lower().ljust(64, "0")[:64],
            dataset_metadata={"document_manifest": {
                "file_id": file_id, "object_id": self.process.object_id, "stage": stage, "section": "АР",
                "relative_path": relative or f"_uploads/{self.project.id}/s3-process/files/{name}",
                "registry": {"file_id": file_id, "doc_stage": stage, "discipline": "АР", "approval_status": approval},
                "registry_required": True, "registry_status": registry_status,
            }},
        )
        self.db.add(doc)
        self.db.flush()
        return doc

    def test_predecessor_chain_selects_only_approved_tip_and_never_keeps_superseded(self):
        old = self.add_document(name="01-АР.pdf", file_id="OLD", approval="SUPERSEDED")
        new = self.add_document(name="01-АР ред.2.pdf", file_id="NEW", approval="APPROVED", predecessor=old.id)
        old.successor_id = new.id
        self.db.commit()

        scopes = file_registry.revision_scopes(self.db, self.process)
        chain = next(scope for scope in scopes if scope["type"] == "PREDECESSOR_CHAIN")
        self.assertEqual(chain["system_status"], "RESOLVED")
        self.assertEqual(chain["system_choice_id"], new.id)
        self.assertEqual(chain["candidates"][0]["approval_status"], "APPROVED")

        selection = file_registry.selection_context(self.db, self.process)
        self.assertIn(old.id, selection["excluded_document_ids"])
        self.assertNotIn(old.id, selection["blocked_by_document"])

    def test_unapproved_scope_requires_clarification_then_inspector_choice_resolves_it(self):
        draft = self.add_document(name="01-АР draft.pdf", file_id="DRAFT", approval="DRAFT")
        self.db.commit()
        scopes = file_registry.revision_scopes(self.db, self.process)
        scope = next(item for item in scopes if item["type"] == "APPROVAL_STATUS")
        selection = file_registry.selection_context(self.db, self.process)
        self.assertEqual(scope["system_status"], "CLARIFICATION_REQUIRED")
        self.assertEqual(selection["blocked_by_document"][draft.id]["reason"], "revision_clarification_required")

        edit = file_registry.InspectorEdit(
            organization_id=self.org.id, project_id=self.project.id, process_id=self.process.id,
            object_id=self.process.object_id, entity_type="REVISION_CHOICE", entity_key=f"revision:{scope['scope_key']}",
            action="CHOOSE", version=1, source_document_version_id=draft.id,
            reason="инспектор подтвердил редакцию", user_id=self.user.id,
        )
        self.db.add(edit)
        self.db.commit()
        selection = file_registry.selection_context(self.db, self.process)
        self.assertNotIn(draft.id, selection["blocked_by_document"])
        with patch("app.domain.v3_jobs.publish_job_message"):
            effect = file_registry.apply_inspector_revision_choice(self.db, self.process, {
                "scope_key": scope["scope_key"], "chosen": {"document_version_id": draft.id}, "rejected": [],
                "inspector_edit_id": edit.id,
            })
        self.assertEqual(effect["status"], "RECOMPUTE_QUEUED")
        self.assertTrue(effect["applied"])

    def test_undated_approved_editions_do_not_resolve_by_partial_dates(self):
        dated = self.add_document(name="01-АР ред.1.pdf", file_id="DATED", approval="APPROVED")
        undated = self.add_document(name="01-АР ред.2.pdf", file_id="UNDATED", approval="APPROVED")
        undated.approval_date = None
        status, choice_id, _basis = file_registry._choose_system_candidate(
            [dated, undated], reason="registry approval dates",
        )
        self.assertEqual(status, "CLARIFICATION_REQUIRED")
        self.assertIsNone(choice_id)

    def test_malformed_predecessor_cycle_requires_clarification(self):
        first = self.add_document(name="01-АР ред.1.pdf", file_id="CHAIN-1", approval="APPROVED")
        second = self.add_document(name="01-АР ред.2.pdf", file_id="CHAIN-2", approval="APPROVED")
        first.predecessor_id, first.successor_id = second.id, second.id
        second.predecessor_id, second.successor_id = first.id, first.id
        self.db.commit()

        scope = next(item for item in file_registry.revision_scopes(self.db, self.process)
                     if item["type"] == "PREDECESSOR_CHAIN")
        self.assertEqual(scope["system_status"], "CLARIFICATION_REQUIRED")
        selection = file_registry.selection_context(self.db, self.process)
        self.assertEqual(selection["blocked_by_document"][first.id]["reason"], "revision_clarification_required")

    def test_reference_to_missing_predecessor_blocks_source(self):
        doc = self.add_document(name="01-АР.pdf", file_id="MISSING-PREDECESSOR", approval="APPROVED",
                                relative=f"_uploads/{self.project.id}/s3-process/files/1/01-АР.pdf")
        self.db.add(AuditLog(
            user_id=self.user_id, action="CASE10_UPLOAD_PACKAGE_IMPORTED", object_id=self.process.object_id,
            project_id=self.project_id, process_id=self.process.id,
            details={"report": {"registry_status": "ACCEPTED", "issues": [{
                "file": "files/1/01-АР.pdf", "issues": ["PREDECESSOR_NOT_IN_PACKAGE"], "ref": "OLD-REV",
            }]}},
        ))
        self.db.commit()

        selection = file_registry.selection_context(self.db, self.process)
        blocked = selection["blocked_by_document"][doc.id]
        self.assertEqual(blocked["reason"], "registry_clarification_required")
        self.assertIn("OLD-REV", blocked["basis"])

    def test_completeness_reports_matrix_sections_and_registry_discipline(self):
        self.add_document(name="01-АР.pdf", file_id="AR1", approval="APPROVED")
        param = Param(
            matrix_version_id=self.matrix.id, project_id=self.project.id, organization_id=self.org.id,
            code="S3-001", section="Раздел АР", parameter_name="Площадь", unit="м²",
            source_pd=True, source_rd=True, source_id=False,
            other_normative=json.dumps({"source_pd": "Раздел АР: таблица", "source_rd": "Раздел АР: лист"}, ensure_ascii=False),
            is_active=True,
        )
        self.db.add(param)
        self.db.commit()

        report = file_registry.completeness_report(self.db, self.process)
        row = next(item for item in report["rows"] if item["stage"] == "PD" and item["section_token"] == "АР")
        self.assertTrue(report["registry_present"])
        self.assertEqual(report["source"], "file_registry")
        self.assertEqual(row["uploaded"], 1)
        self.assertEqual(row["found"], 1)
        self.assertIn("матрица 1.1", row["expected_basis"])
        self.assertEqual(row["status"], "UPLOADED")


class OfflineUploadTests(S3RegistryTestBase):
    def test_pdf_and_registry_uploads_use_batch_plan_without_rag(self):
        import fitz

        from app.db.models import UploadJob

        # The demo profile leaves API_AUTO_PROCESS_UPLOADS disabled. Offline UI uploads must still
        # run through the shared batch importer without a separate queue worker.
        settings.AUTO_PROCESS_UPLOADS = False

        app = FastAPI()
        app.include_router(upload_router, prefix="/api")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.db.get(User, self.user_id)
        client = TestClient(app)

        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text((72, 72), "Раздел АР. Площадь 125,0 м²")
        buffer = io.BytesIO()
        pdf.save(buffer)
        pdf.close()
        pdf_bytes = buffer.getvalue()
        filename = "ПД 01-АР.pdf"
        digest = hashlib.sha256(pdf_bytes).hexdigest()

        with (patch.object(routes_upload, "RagClient", side_effect=AssertionError("RAG must not be constructed")),
              patch.object(routes_upload, "_execute_case10_job_inline", return_value=type("Result", (), {"outcome": "ready"})()) as inline_run):
            uploaded = client.post("/api/upload", data={"project_id": str(self.project_id), "file_type": "auto"},
                                   files={"file": (filename, pdf_bytes, "application/pdf")})
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            process_id = uploaded.json()["process_id"]
            process = self.db.get(InspectionProcess, process_id)
            object_id = process.object_id

            registry_text = ("object_id,file_id,file_name,sha256,doc_stage,discipline,document_code,revision,"
                             "approval_status,approval_date\n"
                             f"{object_id},FILE-1,{filename},{digest},PD,АР,01-АР,1,APPROVED,2026-09-26\n")
            registry = client.post("/api/upload", data={"project_id": str(self.project_id), "file_type": "auto"},
                                   files={"file": ("Перечень.csv", registry_text.encode("utf-8"), "text/csv")})
            self.assertEqual(registry.status_code, 200, registry.text)
        self.assertGreaterEqual(inline_run.call_count, 1)

        document = self.db.query(DocumentVersion).filter(DocumentVersion.dataset_file_id == "FILE-1").one()
        manifest = document.dataset_metadata["document_manifest"]
        self.assertEqual(manifest["registry"]["approval_status"], "APPROVED")
        self.assertEqual(manifest["stage"], "PD")
        relative = manifest["relative_path"]
        self.assertTrue(relative.startswith(f"_uploads/{self.project_id}/{process_id}/files/"))
        self.assertTrue((settings.BATCH_PACKAGES_ROOT / relative).is_file())
        registry_jobs = self.db.query(UploadJob).filter(UploadJob.source_type == "registry").all()
        self.assertEqual(len(registry_jobs), 1)
        self.assertEqual(registry_jobs[0].status, "ready")


class LowQualityTests(S3RegistryTestBase):
    def test_textless_low_confidence_page_is_flagged_and_exported(self):
        import fitz

        from app.domain import dataset_sources
        from app.db.models import SourceFragment

        package = settings.BATCH_PACKAGES_ROOT / "_uploads" / str(self.project_id) / self.process.id / "files"
        package.mkdir(parents=True, exist_ok=True)
        source_path = package / "scan.pdf"
        pdf = fitz.open()
        pdf.new_page()
        pdf.save(source_path)
        pdf.close()
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        relative = source_path.relative_to(settings.BATCH_PACKAGES_ROOT).as_posix()
        os.environ["CASE10_ORIGINALS_ROOT"] = str(settings.BATCH_PACKAGES_ROOT)
        doc = DocumentVersion(
            project_id=self.project_id, organization_id=self.organization_id, source_type="case10_dataset",
            object_id=self.process.object_id, dataset_file_id="SCAN-1", dataset_split="BATCH_PACKAGE",
            dataset_stage="PD", doc_stage="project", document_stage="project", filename="scan.pdf",
            approval_status="APPROVED", content_hash=digest, file_hash=digest,
            dataset_metadata={"document_manifest": {"file_id": "SCAN-1", "relative_path": relative}},
        )
        self.db.add(doc)
        self.db.flush()
        fragment = SourceFragment(document_version_id=doc.id, page=1, text="anchor", source_system="live_tagger",
                                  external_id="LIVE:1:S3-001", metadata_json={"code": "S3-001", "status": "AUTO_FIELD_CANDIDATE",
                                                                             "annotation_type": "MATRIX_FIELD"})
        self.db.add(fragment)
        self.db.flush()

        official_evidence._QUALITY_PAGE_CACHE.clear()
        with patch.object(dataset_sources, "_ocr_page_words", return_value=({"text": "x", "confidence": 22.0},)):
            quality = official_evidence._quality_for_page(doc, 1)
        self.assertEqual(quality["quality_status"], "LOW_QUALITY")
        self.assertIn("NO_TEXT_LAYER_LOW_OCR_CONFIDENCE", quality["reasons"])
        official_evidence._mark_source_fragment_low_quality(self.db, fragment, quality)
        self.db.flush()
        self.assertEqual(fragment.metadata_json["quality_status"], "LOW_QUALITY")

        group = EvidenceGroup(
            process_id=self.process.id, project_id=self.project_id, organization_id=self.organization_id,
            object_id=self.process.object_id, param_id=None, matrix_version=self.process.matrix_version,
            model_version="page-quality-v1", dataset_version=self.process.dataset_version,
            comparison_scenario="SINGLE_ONLY", finding_status="NOT_COMPARABLE", comparability_status="LOW_QUALITY",
            delta={"reason": "LOW_QUALITY", "pages": [{"file_id": "SCAN-1", "stage": "PD", "page": 1,
                                                        "reasons": quality["reasons"], "quality_status": "LOW_QUALITY"}]},
        )
        flags = inspector_workbench.quality_flags(group, [], 0.75)
        self.assertTrue(flags["low_quality"])
        submission = protocol_to_submission({"payload": {"object_id": self.process.object_id, "findings": [{
            "id": 1, "object_id": self.process.object_id, "parameter": {"code": "PZ-001"},
            "finding_status": "NOT_COMPARABLE", "comparability_status": "LOW_QUALITY",
            "delta": {"reason": "LOW_QUALITY", "pages": [{"file_id": "SCAN-1", "stage": "PD", "page": 1,
                                                               "reasons": quality["reasons"], "quality_status": "LOW_QUALITY"}]},
            "fragments": [],
        }]}})
        self.assertEqual(submission["quality_issues"][0]["quality_status"], "LOW_QUALITY")
        self.assertIn("NO_TEXT_LAYER_LOW_OCR_CONFIDENCE", submission["quality_issues"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
