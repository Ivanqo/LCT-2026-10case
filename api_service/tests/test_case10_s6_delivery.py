"""S6 (Phase 12) delivery: server-side batch path, file registry (Perechen ID 1.1), offline profile.

The CLI tests run the REAL pipeline on a tiny synthetic package in a fresh interpreter (settings are read once per
process) with IPv4/IPv6 sockets disabled, and compare the SHA-256 of two runs.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[2]
API = REPO / "api_service"

from app import batch_package as bp  # noqa: E402


def _pdf(path: Path, lines: list[str]) -> None:
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page()
    font = next((f for f in (Path("C:/Windows/Fonts/arial.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
                 if f.is_file()), None)
    if font:
        page.insert_font(fontname="F0", fontfile=str(font))
    for index, line in enumerate(lines):
        page.insert_text((72, 72 + 20 * index), line if font else line.encode("ascii", "replace").decode(),
                         fontname="F0" if font else "helv", fontsize=11)
    doc.save(path)


def _package(root: Path) -> Path:
    _pdf(root / "Проектная документация" / "01-ПЗ.pdf", ["Пояснительная записка", "Площадь застройки 1250,0 м2"])
    _pdf(root / "Рабочая документация" / "АР" / "02-АР.pdf", ["Архитектурные решения", "Площадь застройки 1260,0 м2"])
    _pdf(root / "Исполнительная документация" / "АОСР-1.pdf", ["Акт освидетельствования скрытых работ"])
    return root


def _files(*items: tuple[str, str]) -> list[bp.PackageFile]:
    return [bp.PackageFile(rel, 1, sha) for rel, sha in items]


class RegistryReaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_csv_semicolon_cp1251_aliases_and_normalization(self):
        path = self.tmp / "r.csv"
        text = "Объект;File_ID;Имя файла;SHA-256;Стадия;Марка;Шифр;Редакция;Статус;Дата утверждения\n" \
               "OBJ-1;F1;ПД\\01-ПЗ.pdf;ABCDEF;Стадия П;ПЗ;12-ПЗ;2;approved;12.03.2024\n\n"
        path.write_bytes(text.encode("cp1251"))
        rows = bp.read_registry(path)
        self.assertEqual(rows, [{
            "object_id": "OBJ-1", "file_id": "F1", "file_name": "ПД/01-ПЗ.pdf", "sha256": "abcdef", "doc_stage": "PD",
            "discipline": "ПЗ", "document_code": "12-ПЗ", "revision": "2", "approval_status": "APPROVED",
            "approval_date": "2024-03-12",
        }])

    def test_json_list_and_object_with_inherited_fields(self):
        (self.tmp / "a.json").write_text(json.dumps([{"file_id": "F1", "doc_stage": "РД"}]), encoding="utf-8")
        self.assertEqual(bp.read_registry(self.tmp / "a.json"), [{"file_id": "F1", "doc_stage": "RD"}])
        (self.tmp / "b.json").write_text(json.dumps({"object_id": "O", "files": [{"file_id": "F2", "stage": "ИД"}]}),
                                         encoding="utf-8")
        self.assertEqual(bp.read_registry(self.tmp / "b.json"), [{"object_id": "O", "file_id": "F2", "doc_stage": "ID"}])

    def test_xlsx_shared_strings_and_serial_date(self):
        path = self.tmp / "r.xlsx"
        strings = ["file_id", "doc_stage", "approval_date", "F-7", "PD"]
        shared = "".join(f"<si><t>{s}</t></si>" for s in strings)
        sheet = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
                 '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2" t="s"><v>4</v></c><c r="C2"><v>45363</v></c></row>')
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", f'<workbook {ns} {rel_ns}><sheets><sheet name="Реестр" sheetId="1" r:id="rId9"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                             '<Relationship Id="rId9" Target="worksheets/registry.xml"/></Relationships>')
            archive.writestr("xl/sharedStrings.xml", f"<sst {ns}>{shared}</sst>")
            archive.writestr("xl/worksheets/registry.xml", f"<worksheet {ns}><sheetData>{sheet}</sheetData></worksheet>")
        self.assertEqual(bp.read_registry(path), [{"file_id": "F-7", "doc_stage": "PD", "approval_date": "2024-03-12"}])


class PlanTests(unittest.TestCase):
    def test_no_registry_is_clarification_required_and_stage_from_package_path(self):
        plan = bp.build_plan(_files(("ПД/a.pdf", "a" * 64), ("Рабочая документация/b.pdf", "b" * 64), ("misc/c.pdf", "c" * 64)),
                             None, package_name="Пакет 1")
        self.assertEqual(plan.report["registry_status"], bp.STATUS_CLARIFICATION_REQUIRED)
        self.assertEqual(plan.report["registry_status_reason"], "REGISTRY_MISSING")
        self.assertEqual(plan.object_id, "Пакет-1")
        self.assertEqual([(r["file_id"], r["stage"], r["stage_source"]) for r in plan.rows],
                         [("F-aaaaaaaaaaaaaaaa", "PD", "PACKAGE_PATH"), ("F-bbbbbbbbbbbbbbbb", "RD", "PACKAGE_PATH"),
                          ("F-cccccccccccccccc", "UNKNOWN", "PACKAGE_PATH")])

    def test_registry_matches_by_sha_then_name_and_flags_mismatch(self):
        files = _files(("x/one.pdf", "1" * 64), ("x/two.pdf", "2" * 64), ("x/three.pdf", "3" * 64))
        registry = [
            {"object_id": "O", "file_id": "R1", "sha256": "1" * 64, "doc_stage": "PD", "discipline": "ПЗ", "approval_status": "APPROVED"},
            {"object_id": "O", "file_id": "R2", "file_name": "two.pdf", "sha256": "f" * 64, "doc_stage": "RD"},
            {"object_id": "O", "file_id": "R9", "file_name": "gone.pdf", "doc_stage": "ID"},
        ]
        plan = bp.build_plan(files, registry, package_name="p", registry_sha256="r" * 64)
        rows = {r["file_id"]: r for r in plan.rows}
        self.assertEqual(rows["R1"]["stage"], "PD")
        self.assertEqual(rows["R1"]["section"], "ПЗ")
        self.assertEqual(rows["R1"]["approval_status"], "APPROVED")
        self.assertEqual(rows["R2"]["registry_issues"], ["SHA256_MISMATCH"])
        self.assertEqual(rows["F-3333333333333333"]["registry_issues"], ["NOT_IN_REGISTRY"])
        self.assertEqual(plan.report["registry_status"], bp.STATUS_CLARIFICATION_REQUIRED)
        self.assertEqual(plan.report["registry_rows_without_file"], [{"file_id": "R9", "file_name": "gone.pdf", "doc_stage": "ID"}])

    def test_complete_registry_is_accepted_and_revision_chain_linked(self):
        files = _files(("a.pdf", "1" * 64), ("b.pdf", "2" * 64))
        registry = [{"file_id": "OLD", "sha256": "1" * 64, "doc_stage": "RD", "successor_id": "NEW", "approval_status": "SUPERSEDED"},
                    {"file_id": "NEW", "sha256": "2" * 64, "doc_stage": "RD", "predecessor_id": "OLD", "approval_status": "APPROVED"}]
        plan = bp.build_plan(files, registry, package_name="p", object_id="O", registry_sha256="r" * 64)
        self.assertEqual(plan.report["registry_status"], bp.STATUS_ACCEPTED)
        self.assertEqual(plan.links, [("NEW", "predecessor", "OLD"), ("OLD", "successor", "NEW")])

    def test_several_objects_need_an_explicit_choice(self):
        registry = [{"object_id": "A", "file_id": "1"}, {"object_id": "B", "file_id": "2"}]
        with self.assertRaises(bp.PackageError):
            bp.build_plan(_files(("a.pdf", "1" * 64)), registry, package_name="p", registry_sha256="r")
        plan = bp.build_plan(_files(("a.pdf", "1" * 64)), registry, package_name="p", object_id="B", registry_sha256="r")
        self.assertEqual(plan.object_id, "B")

    def test_folder_the_package_sits_in_never_decides_the_stage(self):
        files = _files(("Проектная документация/a.pdf", "1" * 64))
        plan = bp.build_plan(files, None, package_name="p", originals_prefix="РД входящие/p")
        self.assertEqual(plan.rows[0]["stage"], "PD")
        self.assertEqual(plan.rows[0]["relative_path"], "РД входящие/p/Проектная документация/a.pdf")
        self.assertEqual(plan.report["files"][0]["relative_path"], "Проектная документация/a.pdf")

    def test_identical_content_without_registry_is_read_once(self):
        plan = bp.build_plan(_files(("a/x.pdf", "1" * 64), ("b/x.pdf", "1" * 64)), None, package_name="p")
        self.assertEqual(len(plan.rows), 1)
        self.assertEqual(plan.report["issues"][0]["issues"], ["DUPLICATE_CONTENT"])


class PackageOnDiskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_is_sorted_hashed_and_skips_junk(self):
        root = self.tmp / "pkg"
        for rel in ("b/2.pdf", "a/1.pdf", "__MACOSX/a/._1.pdf", "a/Thumbs.db", ".hidden"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(rel.encode())
        files = bp.scan_package(root)
        self.assertEqual([f.relative_path for f in files], ["a/1.pdf", "b/2.pdf"])
        self.assertEqual(files[0].sha256, hashlib.sha256(b"a/1.pdf").hexdigest())

    def test_pdf_page_count_reaches_the_manifest_row_the_tagger_budgets_from(self):
        """Without `pdf_pages` every document counts as 60 pages in the live tagger's plan: long documents are cut
        at page 60 and a package of 500+ PDFs exhausts the 30 000-page budget (found on DOO25 in Docker)."""
        import fitz

        from app.domain.dataset_sources import document_page_count_hint

        root = self.tmp / "pkg"
        root.mkdir()
        doc = fitz.open()
        for _ in range(75):
            doc.new_page()
        doc.save(root / "long.pdf")
        (root / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
        files = bp.scan_package(root)
        self.assertEqual({f.relative_path: f.pdf_pages for f in files}, {"broken.pdf": None, "long.pdf": 75})
        plan = bp.build_plan(files, None, package_name="p")
        row = next(r for r in plan.rows if r["package_path"] == "long.pdf")

        class _Doc:
            dataset_metadata = {"document_manifest": row}

        self.assertEqual(document_page_count_hint(_Doc()), 75)
        self.assertEqual(plan.report["pdf_pages_total"], 75)
        self.assertEqual(plan.report["pdf_unreadable"], ["broken.pdf"])

    def test_zip_slip_members_are_dropped_and_unpack_is_reused(self):
        archive_path = self.tmp / "p.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("Объект/ПД/a.pdf", b"a")
            archive.writestr("../evil.pdf", b"x")
            archive.writestr("/abs.pdf", b"x")
            archive.writestr("C:/drive.pdf", b"x")
        root, name = bp.resolve_package(archive_path, unpack_root=self.tmp / "unpacked")
        self.assertEqual(name, "p")
        self.assertEqual([f.relative_path for f in bp.scan_package(root)], ["Объект/ПД/a.pdf"])
        self.assertFalse((self.tmp / "evil.pdf").exists())
        (root / "Объект" / "ПД" / "a.pdf").write_bytes(b"changed")
        again, _ = bp.resolve_package(archive_path, unpack_root=self.tmp / "unpacked")
        self.assertEqual(again, root)
        self.assertEqual((again / "Объект" / "ПД" / "a.pdf").read_bytes(), b"changed")   # not unpacked twice

    def test_unpacked_size_guard(self):
        archive_path = self.tmp / "big.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("a.pdf", b"0" * 10_000)
        with self.assertRaises(bp.PackageError):
            bp.resolve_package(archive_path, unpack_root=self.tmp / "u", max_unpacked_bytes=1_000)


def _child_env(tmp: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CASE10_") and k not in ("API_DATA_DIR",)}
    env.update({
        "PYTHONPATH": os.pathsep.join([str(REPO), str(API)]),
        "PYTHONIOENCODING": "utf-8",
        "API_DATA_DIR": str(tmp / "data"),
        "CASE10_LLM_VERIFIER_ENABLED": "0",
    })
    env.update(extra)
    return env


_GUARDED_RUN = textwrap.dedent("""
    import json, socket, sys
    attempts = []
    def _refuse(kind):
        def blocked(*args, **kwargs):
            attempts.append([kind, repr(args[1:] if kind.startswith("socket.") else args)[:200]])
            raise OSError("network disabled by test: " + kind)
        return blocked
    _connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            return _refuse("socket.connect")(self, address)
        return _connect(self, address)
    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            return _refuse("socket.connect_ex")(self, address)
        return _connect_ex(self, address)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.create_connection = _refuse("create_connection")
    socket.getaddrinfo = _refuse("getaddrinfo")
    from app.cli import main
    code = main(sys.argv[1:])
    forbidden = sorted(m for m in sys.modules if m == "shared.llm" or m.startswith(("app.clients", "app.api", "app.services", "httpx", "pika", "redis")))
    print("GUARD " + json.dumps({"code": code, "attempts": attempts, "forbidden_modules": forbidden}))
""")


class CliEndToEndTests(unittest.TestCase):
    """Real pipeline, real (synthetic) PDFs, no network: the ТЗ 1.1 JSON is valid and byte-reproducible."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.package = _package(cls.tmp / "Пакет")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run(self, *args: str, guarded: bool = False, **env: str) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-c", _GUARDED_RUN, *args] if guarded else [sys.executable, "-m", "app.cli", *args]
        return subprocess.run(cmd, cwd=self.tmp, env=_child_env(self.tmp, **env), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=900)

    def test_offline_run_valid_and_reproducible(self):
        first = self._run("run", "--docs", str(self.package), "--out", str(self.tmp / "r1" / "result.json"),
                          "--workers", "1", "--timings", str(self.tmp / "r1" / "timings.json"), guarded=True)
        self.assertEqual(first.returncode, 0, first.stderr[-3000:])
        guard = json.loads(next(line for line in first.stdout.splitlines() if line.startswith("GUARD "))[6:])
        self.assertEqual(guard["code"], 0)
        self.assertEqual(guard["attempts"], [], "the offline CLI path tried to open a network connection")
        self.assertEqual(guard["forbidden_modules"], [], "the CLI path imported a network client / legacy module")

        result = json.loads((self.tmp / "r1" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(len(result["checks"]), 132)
        self.assertEqual(result["package"]["registry_status"], "CLARIFICATION_REQUIRED")
        self.assertEqual(result["package"]["documents_by_stage"], {"ID": 1, "PD": 1, "RD": 1})
        self.assertTrue(all(check["parameter_code"].startswith("M-") for check in result["checks"]))
        timings = json.loads((self.tmp / "r1" / "timings.json").read_text(encoding="utf-8"))
        self.assertEqual(timings["validation_errors"], [])
        for stage in ("package_scan_sha256", "import", "tagging", "compare_132", "protocol", "export_validate"):
            self.assertIn(stage, timings["stages_seconds"])

        validate = self._run("validate", str(self.tmp / "r1" / "result.json"))
        self.assertEqual(validate.returncode, 0, validate.stderr)

        # same package as a ZIP (other name, other place, 2 workers): identical bytes
        archive = self.tmp / "copy.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
            for path in sorted(self.package.rglob("*.pdf")):
                out.write(path, path.relative_to(self.package).as_posix())
        second = self._run("run", "--docs", str(archive), "--object-id", "Пакет", "--out", str(self.tmp / "r2" / "result.json"),
                           "--workers", "2")
        self.assertEqual(second.returncode, 0, second.stderr[-3000:])
        first_bytes = (self.tmp / "r1" / "result.json").read_bytes()
        second_json = json.loads((self.tmp / "r2" / "result.json").read_bytes())
        second_json["package"]["package_name"] = result["package"]["package_name"]
        self.assertEqual(json.loads(first_bytes)["checks"], second_json["checks"])
        self.assertEqual(json.loads(first_bytes), second_json)

    def test_registry_run_and_schema_violation_exit_code(self):
        registry = self.tmp / "registry.csv"
        with registry.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["object_id", "file_id", "file_name", "sha256", "doc_stage", "discipline", "approval_status"])
            for index, path in enumerate(sorted(self.package.rglob("*.pdf")), 1):
                rel = path.relative_to(self.package).as_posix()
                stage = "PD" if rel.startswith("Проект") else "RD" if rel.startswith("Рабоч") else "ID"
                writer.writerow(["OBJ-S6", f"S6-{index:03d}", rel, hashlib.sha256(path.read_bytes()).hexdigest(), stage, "АР", "APPROVED"])
        out = self.tmp / "r3" / "result.json"
        done = self._run("run", "--docs", str(self.package), "--registry", str(registry), "--out", str(out), "--workers", "1")
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        result = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(result["object_id"], "OBJ-S6")
        self.assertEqual(result["package"]["registry_status"], "ACCEPTED")
        self.assertEqual({f["file_id"] for f in result["package"]["files"]}, {"S6-001", "S6-002", "S6-003"})

        broken = dict(result)
        broken["checks"] = [{"parameter_code": "M-001", "location": "x", "violation_label": "MAYBE", "evidence": []}]
        (self.tmp / "broken.json").write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self._run("validate", str(self.tmp / "broken.json")).returncode, 2)


class OfflineDeliveryTests(unittest.TestCase):
    def test_delivery_app_does_not_mount_or_import_chat_and_refuses_services(self):
        code = textwrap.dedent("""
            import asyncio, json, socket, sys
            attempts = []
            def refuse(*a, **k):
                attempts.append(repr(a)[:120]); raise OSError("network disabled by test")
            socket.create_connection = refuse; socket.getaddrinfo = refuse
            import app.main as m
            from fastapi import HTTPException
            from app.clients.rag_client import RagClient
            from app.clients.ifc_client import IfcClient
            from app.services import simple_documents
            refused = []
            for make in (lambda: RagClient(), lambda: IfcClient(),
                         lambda: asyncio.run(simple_documents._proxy_chat([{"role": "user", "content": "x"}])),
                         lambda: asyncio.run(simple_documents._create_proxy_chat("x"))):
                try:
                    make()
                except HTTPException as exc:
                    refused.append(exc.status_code)
            paths = sorted({r.path for r in m.app.routes})
            print("OUT " + json.dumps({
                "chat_or_rag_routes": [p for p in paths if p.startswith(("/api/chat", "/api/rag/"))],
                "batch_routes": [p for p in paths if p.startswith("/api/case10/batch-runs")],
                "qwen_modules": [x for x in ("shared.llm", "app.api.routes_chat", "app.api.routes_rag_assets") if x in sys.modules],
                "refused": refused, "attempts": attempts}))
        """)
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, "-c", code], cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=300,
                                  env=_child_env(Path(tmp), CASE10_OFFLINE_DELIVERY="1"))
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        out = json.loads(next(line for line in done.stdout.splitlines() if line.startswith("OUT "))[4:])
        self.assertEqual(out["chat_or_rag_routes"], [])
        self.assertEqual(out["qwen_modules"], [])
        self.assertEqual(out["refused"], [503, 503, 503, 503])
        self.assertEqual(out["attempts"], [])
        self.assertEqual(len(out["batch_routes"]), 3)


class BatchRunEndpointTests(unittest.TestCase):
    """POST /api/case10/batch-runs imports a server-side package into the service DB so the worker (reading bytes
    through CASE10_ORIGINALS_ROOT) and the UI see it; paths outside the packages root are refused."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.api.routes_batch import router
        from app.config import settings
        from app.db.models import Organization, Project, User
        from app.db.session import Base, get_db

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "packages"
        _package(self.root / "Объект А")
        self.settings = settings
        self._old = (settings.BATCH_PACKAGES_ROOT, settings.LIVE_TAGGER_ENABLED, os.environ.get("CASE10_ORIGINALS_ROOT"))
        settings.BATCH_PACKAGES_ROOT = self.root.resolve()
        settings.LIVE_TAGGER_ENABLED = False     # keep the endpoint test fast; the CLI test runs the tagger
        os.environ["CASE10_ORIGINALS_ROOT"] = str(self.root)

        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        org = Organization(name="S6 Org", slug="s6-org")
        self.db.add(org)
        self.db.flush()
        self.user = User(login="s6", email="s6@example.test", password_hash="x", api_key="s6-key", is_admin=True,
                         role="ADMIN", organization_id=org.id)
        self.project = Project(name="S6 Project", organization_id=org.id)
        self.db.add_all([self.user, self.project])
        self.db.commit()
        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.headers = {"X-API-Key": "s6-key"}

    def tearDown(self):
        self.db.close()
        root, tagger, originals = self._old
        self.settings.BATCH_PACKAGES_ROOT, self.settings.LIVE_TAGGER_ENABLED = root, tagger
        if originals is None:
            os.environ.pop("CASE10_ORIGINALS_ROOT", None)
        else:
            os.environ["CASE10_ORIGINALS_ROOT"] = originals
        self._tmp.cleanup()

    def test_paths_outside_the_root_are_refused(self):
        for package in ("../x", "/etc", "C:/Windows", "missing"):
            response = self.client.post("/api/case10/batch-runs", headers=self.headers,
                                        json={"project_id": self.project.id, "package": package, "run_immediately": False})
            self.assertIn(response.status_code, (404, 422), package)

    def test_import_then_run_gives_readable_documents_and_a_valid_result(self):
        from app.db.models import DocumentVersion
        from app.domain.dataset_sources import original_document_bytes
        from app.domain.v3_pipeline import run_process

        response = self.client.post("/api/case10/batch-runs", headers=self.headers,
                                    json={"project_id": self.project.id, "package": "Объект А", "run_immediately": False})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["documents_imported"], 3)
        self.assertEqual(body["package"]["registry_status"], "CLARIFICATION_REQUIRED")
        docs = self.db.query(DocumentVersion).filter(DocumentVersion.project_id == self.project.id).all()
        self.assertEqual(sorted(d.doc_stage for d in docs), ["as_built", "project", "working"])
        for doc in docs:   # what the worker will read: through CASE10_ORIGINALS_ROOT, SHA-256 checked
            self.assertTrue(original_document_bytes(doc).startswith(b"%PDF"))

        process_id = body["process"]["process_id"]
        self.assertEqual(self.client.get(f"/api/case10/batch-runs/{process_id}/result", headers=self.headers).status_code, 409)
        run_process(self.db, process_id=process_id)
        result = self.client.get(f"/api/case10/batch-runs/{process_id}/result", headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json()["checks"]), 132)
        self.assertEqual(result.json()["package"]["object_id"], "Объект-А")
        status = self.client.get(f"/api/case10/batch-runs/{process_id}", headers=self.headers).json()
        self.assertEqual(status["package"]["files_listed"], 3)

    def test_misconfigured_originals_root_is_503(self):
        os.environ["CASE10_ORIGINALS_ROOT"] = str(Path(self._tmp.name))
        response = self.client.post("/api/case10/batch-runs", headers=self.headers,
                                    json={"project_id": self.project.id, "package": "Объект А"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
