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
from app.api.routes_upload import router as upload_router
from app.db.models import AuditLog, Organization, Project, User
from app.db.session import Base, get_db


class UploadSecurityTests(unittest.TestCase):
    """Covers three ТЗ 9.1/12 upload-slice security gaps: audit log IP capture
    (via the real, un-overridden get_current_user dependency, so requests are
    authenticated with a real X-API-Key header rather than a test override),
    and the per-file/per-package size limits plus the unsupported-format
    message on the real /upload endpoint."""

    def setUp(self):
        self._tmp_uploads = tempfile.TemporaryDirectory()
        self._old_temp_uploads_dir = settings.TEMP_UPLOADS_DIR
        settings.TEMP_UPLOADS_DIR = Path(self._tmp_uploads.name)
        self._old_max_file = settings.MAX_UPLOAD_FILE_SIZE_BYTES
        self._old_max_package = settings.MAX_UPLOAD_PACKAGE_SIZE_BYTES
        settings.MAX_UPLOAD_FILE_SIZE_BYTES = 1000
        settings.MAX_UPLOAD_PACKAGE_SIZE_BYTES = 1500

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()

        org = Organization(name="Upload Security Org", slug="upload-security-org")
        self.db.add(org)
        self.db.flush()
        self.user = User(
            login="upload-security-user", email="uploader@example.test", password_hash="x",
            api_key="upload-security-key", is_admin=False, role="INSPECTOR", organization_id=org.id,
        )
        self.project = Project(name="Upload Security Project A", organization_id=org.id)
        self.project2 = Project(name="Upload Security Project B", organization_id=org.id)
        self.db.add_all([self.user, self.project, self.project2])
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(self.project)
        self.db.refresh(self.project2)

        app = FastAPI()
        app.include_router(upload_router, prefix="/api")

        def override_db():
            try:
                yield self.db
            finally:
                pass

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        settings.TEMP_UPLOADS_DIR = self._old_temp_uploads_dir
        settings.MAX_UPLOAD_FILE_SIZE_BYTES = self._old_max_file
        settings.MAX_UPLOAD_PACKAGE_SIZE_BYTES = self._old_max_package
        self._tmp_uploads.cleanup()

    def _upload(self, project_id: int, filename: str, content: bytes, *, headers: dict | None = None, file_type: str = "xml"):
        merged_headers = {"X-API-Key": self.user.api_key}
        if headers:
            merged_headers.update(headers)
        return self.client.post(
            "/api/upload",
            data={"project_id": str(project_id), "file_type": file_type},
            files={"file": (filename, content, "application/xml")},
            headers=merged_headers,
        )

    # -- audit log IP capture (task 2) ------------------------------------

    def test_audit_log_captures_first_ip_from_x_forwarded_for(self):
        resp = self._upload(
            self.project.id, "doc1.xml", b"<root>a</root>",
            headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.5"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        process_id = resp.json()["process_id"]
        audit = (
            self.db.query(AuditLog)
            .filter(AuditLog.process_id == str(process_id), AuditLog.action == "PROCESS_CREATED")
            .first()
        )
        self.assertIsNotNone(audit)
        self.assertEqual(audit.ip_address, "203.0.113.9")

    def test_audit_log_falls_back_to_client_host_without_forwarded_header(self):
        resp = self._upload(self.project2.id, "doc2.xml", b"<root>b</root>")
        self.assertEqual(resp.status_code, 200, resp.text)
        process_id = resp.json()["process_id"]
        audit = (
            self.db.query(AuditLog)
            .filter(AuditLog.process_id == str(process_id), AuditLog.action == "PROCESS_CREATED")
            .first()
        )
        self.assertIsNotNone(audit)
        self.assertEqual(audit.ip_address, "testclient")

    # -- per-file size limit (task 3) --------------------------------------

    def test_oversized_file_is_rejected_with_413(self):
        oversized = b"x" * (settings.MAX_UPLOAD_FILE_SIZE_BYTES + 1)
        resp = self._upload(self.project.id, "big.xml", oversized)
        self.assertEqual(resp.status_code, 413, resp.text)
        self.assertIn("максимально допустимый размер", resp.json()["detail"])

    def test_file_at_exact_limit_is_accepted(self):
        exact = b"x" * settings.MAX_UPLOAD_FILE_SIZE_BYTES
        resp = self._upload(self.project.id, "exact.xml", exact)
        self.assertEqual(resp.status_code, 200, resp.text)

    # -- package size limit (task 3) ---------------------------------------

    def test_package_total_over_limit_is_rejected_even_though_each_file_is_within_the_per_file_limit(self):
        first = self._upload(self.project.id, "part1.xml", b"a" * 800)
        self.assertEqual(first.status_code, 200, first.text)

        second = self._upload(self.project.id, "part2.xml", b"b" * 800)
        self.assertEqual(second.status_code, 413, second.text)
        self.assertIn("пакета", second.json()["detail"])

    def test_package_limit_is_scoped_per_project(self):
        first = self._upload(self.project.id, "part1.xml", b"a" * 800)
        self.assertEqual(first.status_code, 200, first.text)

        other_project = self._upload(self.project2.id, "part1.xml", b"a" * 800)
        self.assertEqual(other_project.status_code, 200, other_project.text)

    # -- unsupported format message (task 3) --------------------------------

    def test_unsupported_format_message_lists_pdf_docx_xml(self):
        resp = self._upload(self.project.id, "notes.zip", b"not really a zip", file_type="auto")
        self.assertEqual(resp.status_code, 400, resp.text)
        detail = resp.json()["detail"]
        self.assertIn("PDF", detail)
        self.assertIn("DOCX", detail)
        self.assertIn("XML", detail)


if __name__ == "__main__":
    unittest.main()
