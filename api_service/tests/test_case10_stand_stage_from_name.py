"""D3: the stage of a file uploaded through the interface is read from its own name (deterministic, conservative)."""
from __future__ import annotations

import hashlib
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_upload
from app.api.auth import get_current_user
from app.api.routes_case10 import router as case10_router
from app.api.routes_upload import router as upload_router
from app.config import settings
from app.db.models import DocumentVersion, User
from app.db.session import get_db
from app.domain.stage_from_name import stage_from_name
import tests.test_case10_s3_file_registry as _s3

S3RegistryTestBase = _s3.S3RegistryTestBase


NAME_TABLE = [
    # clear single stage -> assigned, source FILENAME
    ("ПД 01-АР.pdf", "PD"), ("РД 01-АР.pdf", "RD"), ("ИД 01-АР.pdf", "ID"),
    ("ПД_Раздел 5_ОВ.pdf", "PD"), ("РД_Раздел_5_ОВ.pdf", "RD"), ("ИД_АОСР_12.pdf", "ID"),
    ("пд раздел 1.pdf", "PD"), ("Рд-3 планы.pdf", "RD"), ("ид (акты).pdf", "ID"),
    ("PD_section_1.pdf", "PD"), ("RD section 2.pdf", "RD"), ("ID-act-3.pdf", "ID"),
    ("Проектная документация. Раздел АР.pdf", "PD"),
    ("Рабочая документация. Планы.pdf", "RD"),
    ("Исполнительная документация. АОСР.pdf", "ID"),
    ("Исполнительный чертеж ОВ.pdf", "ID"),
    ("as-built drawings.pdf", "ID"),
    ("РД1 ОВ.pdf", "RD"),
    ("Объект 12 (ПД).pdf", "PD"),
    # ZIP folder names
    ("Проектная документация/АР/план.pdf", "PD"),
    ("Комплект/РД/ОВ/план.pdf", "RD"),
    ("ИД\АОСР\1.pdf", "ID"),
    ("Рабочая/Исполнительная.pdf", "RD"),  # outermost stage folder wins, nothing is merged
    ("ПД/РД_заметка.pdf", "PD"),  # the outer folder names the stage, the file name does not override it
]
UNASSIGNED = [
    ("ПД_РД 01.pdf", True), ("ПД РД ИД.pdf", True), ("Рабочая и исполнительная документация.pdf", True),
    ("РД и ИД.pdf", True), ("Проектная и рабочая.pdf", True),
    ("План этажа.pdf", False), ("Раздел АР 1.pdf", False), ("scan_0001.pdf", False), ("", False),
    ("doc_id123.pdf", False),  # a Latin id with digits is not a stage
    ("Рядовой проект.pdf", False),  # «проект» alone is not «проектная документация»
    ("Предисловие.pdf", False), ("Рдест.pdf", False), ("Идея.pdf", False),  # no substring matches
]


@pytest.mark.parametrize("name,expected", NAME_TABLE)
def test_name_table_assigns_one_stage(name, expected):
    guess = stage_from_name(name)
    assert guess.stage == expected, guess
    assert guess.source in {"FILENAME", "FOLDER"}
    assert guess.warning is None


@pytest.mark.parametrize("name,ambiguous", UNASSIGNED)
def test_name_table_never_guesses(name, ambiguous):
    guess = stage_from_name(name)
    assert guess.stage is None and guess.source is None
    assert bool(guess.warning) is ambiguous


def test_detection_is_deterministic():
    names = [name for name, _ in NAME_TABLE] + [name for name, _ in UNASSIGNED]
    assert [stage_from_name(n) for n in names] == [stage_from_name(n) for n in names]


def _pdf(text: str) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    return buffer.getvalue()


class InterfaceUploadStageTests(S3RegistryTestBase):
    def _client(self):
        app = FastAPI()
        app.include_router(upload_router, prefix="/api")
        app.include_router(case10_router, prefix="/api")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.db.get(User, self.user_id)
        return TestClient(app)

    def _upload(self, client, name: str, payload: bytes):
        from unittest.mock import patch

        with patch.object(routes_upload, "_execute_case10_job_inline", return_value=type("R", (), {"outcome": "ready"})()):
            response = client.post("/api/upload", data={"project_id": str(self.project_id), "file_type": "auto"},
                                   files={"file": (name, payload, "application/pdf")})
        assert response.status_code == 200, response.text
        return response.json()

    def test_four_files_get_stages_from_their_names_with_source_and_warning(self):
        settings.AUTO_PROCESS_UPLOADS = False
        client = self._client()
        names = ["ПД Раздел 1 АР.pdf", "РД Раздел 1 АР.pdf", "ИД АОСР 1.pdf", "ПД_РД Сводная ведомость.pdf"]
        for index, name in enumerate(names):
            self._upload(client, name, _pdf(f"Документ {index}. Площадь {100 + index},0 м²"))
        body = client.get(f"/api/case10/documents?project_id={self.project_id}").json()
        by_name = {doc["filename"]: (group["stage"], doc) for group in body["groups"] for doc in group["documents"]}
        assert by_name["ПД Раздел 1 АР.pdf"][0] == "project"
        assert by_name["РД Раздел 1 АР.pdf"][0] == "working"
        assert by_name["ИД АОСР 1.pdf"][0] == "as_built"
        for name in names[:3]:
            doc = by_name[name][1]
            assert doc["stage_source"] == "FILENAME" and doc["stage_source_label"] == "по имени файла"
            assert doc["stage_warning"] is None
        stage, ambiguous = by_name["ПД_РД Сводная ведомость.pdf"]
        assert stage == "unknown"
        assert ambiguous["stage_warning"] and "ПД" in ambiguous["stage_warning"] and "РД" in ambiguous["stage_warning"]
        assert ambiguous["stage_source_label"] is None
        # the documents carry PD/RD/ID for the check, not "everything unspecified"
        stages = {doc.dataset_stage for doc in self.db.query(DocumentVersion).filter(DocumentVersion.project_id == self.project_id)}
        assert {"PD", "RD", "ID", "UNKNOWN"} == stages

    def test_registry_stage_and_stage_folder_are_not_overridden_by_the_file_name(self):
        from app import batch_package as bp

        files = [bp.PackageFile("files/ПД/1/РД scan.pdf", 10, "a" * 64, 1),
                 bp.PackageFile("files/2/РД plan.pdf", 10, "b" * 64, 1)]
        registry = [{"file_name": "files/2/РД plan.pdf", "doc_stage": "ID", "file_id": "R2"}]
        plan = bp.build_plan(files, registry, package_name="x", object_id="OBJ", stage_fallback=stage_from_name)
        by_path = {row["package_path"]: row for row in plan.rows}
        assert by_path["files/ПД/1/РД scan.pdf"]["stage"] == "PD"          # folder wins over the name
        assert by_path["files/ПД/1/РД scan.pdf"]["stage_source"] == "PACKAGE_PATH"
        assert by_path["files/2/РД plan.pdf"]["stage"] == "ID"             # registry wins over the name
        assert by_path["files/2/РД plan.pdf"]["stage_source"] == "REGISTRY"

    def test_batch_path_without_fallback_is_unchanged(self):
        from app import batch_package as bp

        files = [bp.PackageFile("files/1/РД plan.pdf", 10, "c" * 64, 1)]
        plan = bp.build_plan(files, [], package_name="x", object_id="OBJ")
        assert plan.rows[0]["stage"] == "UNKNOWN" and plan.rows[0]["stage_source"] == "PACKAGE_PATH"
        assert "stage_warning" not in plan.rows[0]
