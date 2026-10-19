"""Small synthetic checks for robustness paths; no corpus runs are started here."""
from __future__ import annotations

import os
from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

from app import batch_package as bp, cli

REPO = Path(__file__).resolve().parents[2]
API = REPO / "api_service"
sys.path.insert(0, str(REPO / "evaluation" / "phase12"))
import rob_harness  # noqa: E402


class RegistryInputFailureTests(unittest.TestCase):
    def setUp(self):
        self.old_robust = os.environ.get("CASE10_ROBUST_INPUTS")
        os.environ["CASE10_ROBUST_INPUTS"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()

    def tearDown(self):
        self.tmp.cleanup()
        if self.old_robust is None:
            os.environ.pop("CASE10_ROBUST_INPUTS", None)
        else:
            os.environ["CASE10_ROBUST_INPUTS"] = self.old_robust

    def test_corrupt_json_and_xlsx_are_package_errors(self):
        for name, payload in (("bad.json", b"{not json"), ("bad.xlsx", b"not a zip archive")):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(payload)
                with self.assertRaisesRegex(bp.PackageError, rf"cannot read registry .*{name}"):
                    bp.read_registry(path)

    def test_cli_reports_corrupt_registry_without_traceback(self):
        registry = self.root / "broken.json"
        registry.write_text("{not json", encoding="utf-8")
        out = self.root / "result.json"
        env = os.environ.copy()
        env.update({"PYTHONPATH": f"{API};{REPO}", "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1"})
        command = [sys.executable, "-m", "app.cli", "run", "--docs", str(self.root / "docs"),
                   "--registry", str(registry), "--out", str(out)]
        completed = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=15, check=False)
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 1, output)
        self.assertIn("error: cannot read registry", output.lower())
        self.assertNotIn("Traceback (most recent call last)", output)
        self.assertFalse(out.exists())

    def test_cli_turns_unexpected_input_failure_into_a_documented_error(self):
        output = StringIO()
        with patch("app.cli._run", side_effect=RuntimeError("synthetic page read failure")):
            with redirect_stderr(output):
                code = cli.main(["run", "--docs", str(self.root / "docs"), "--out", str(self.root / "result.json")])
        self.assertEqual(code, 1)
        self.assertIn("error: batch run failed (RuntimeError): synthetic page read failure", output.getvalue())
        self.assertNotIn("Traceback (most recent call last)", output.getvalue())

    def test_cli_prints_missing_registry_rows_files_and_directory_scan_warnings(self):
        output = StringIO()
        report = {
            "registry_warnings": [
                {"code": "REGISTRY_ROW_WITHOUT_FILE", "count": 2,
                 "rows": [{"file_name": "missing-a.pdf"}, {"file_name": "missing-b.pdf"}]},
                {"code": "FILE_WITHOUT_REGISTRY_ROW", "count": 1, "files": ["PD/unlisted.pdf"]},
            ],
            "scan_errors": [{"file": "long-directory", "reason": "DIRECTORY_READ_ERROR"}],
        }
        with redirect_stderr(output):
            cli._print_package_warnings(report)
        text = output.getvalue().lower()
        self.assertIn("2 registry rows without a matching file", text)
        self.assertIn("1 package files without a registry row", text)
        self.assertIn("directory_read_error", text)

    def test_cli_returns_a_valid_protocol_with_unsupported_file_reason(self):
        (self.root / "docs" / "notes.unsupported").write_bytes(b"not a supported document")
        out = self.root / "unsupported-result.json"
        env = os.environ.copy()
        env.update({"PYTHONPATH": f"{API};{REPO}", "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1", "CASE10_LIVE_TAGGER_WORKERS": "1"})
        command = [sys.executable, "-m", "app.cli", "run", "--docs", str(self.root / "docs"),
                   "--out", str(out), "--work-dir", str(self.root / "work"), "--workers", "1"]
        completed = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=20, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(result["checks"]), 132)
        self.assertEqual(result["package"]["excluded_files"], [
            {"file": "notes.unsupported", "reason": "UNSUPPORTED_EXTENSION"},
        ])


class SyntheticMutationTests(unittest.TestCase):
    def test_robust_inputs_default_on_after_gate(self):
        from app.robustness import robust_inputs_enabled, default_max_unpacked_bytes

        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(robust_inputs_enabled())
            self.assertEqual(default_max_unpacked_bytes(), 8 * 1024**3)

    def test_robust_input_flag_switches_scanner_behavior_and_zip_limit(self):
        docs = self.root / "flag-package"
        docs.mkdir()
        path = docs / "notes.bin"
        path.write_bytes(b"payload")
        with patch.dict(os.environ, {"CASE10_ROBUST_INPUTS": "0"}):
            legacy = bp.scan_package(docs)[0]
            self.assertEqual(legacy.sha256, bp.sha256_file(path))
            self.assertIsNone(legacy.excluded_reason)
            from app.robustness import default_max_unpacked_bytes
            self.assertEqual(default_max_unpacked_bytes(), 256 * 1024**3)
        with patch.dict(os.environ, {"CASE10_ROBUST_INPUTS": "1"}):
            robust = bp.scan_package(docs)[0]
            self.assertEqual(robust.sha256, "")
            self.assertEqual(robust.excluded_reason, "UNSUPPORTED_EXTENSION")
            self.assertEqual(default_max_unpacked_bytes(), 8 * 1024**3)

    def test_matrix_coverage_accepts_multiple_location_rows_for_132_codes(self):
        checks = [{"matrix_code": f"M-{number:03d}"} for number in range(1, 133)]
        checks.extend({"matrix_code": "M-001"} for _ in range(12))
        seen, missing, unexpected = rob_harness._matrix_code_coverage(checks)
        self.assertEqual(len(checks), 144)
        self.assertEqual(len(seen), 132)
        self.assertFalse(missing)
        self.assertFalse(unexpected)

    def test_robust_export_collapses_same_code_without_discarding_occurrences(self):
        from evaluation.exporter import _collapse_matrix_checks

        checks = [{"matrix_code": f"M-{number:03d}", "evidence": []} for number in range(1, 133)]
        checks[0] = {"matrix_code": "M-001", "violation_label": "COMPARISON_IMPOSSIBLE",
                     "evidence": [{"file_id": "F1", "page": 1}], "finding_id": "F1"}
        checks.append({"matrix_code": "M-001", "violation_label": "COMPARISON_IMPOSSIBLE",
                       "evidence": [], "finding_id": "F2"})
        first = _collapse_matrix_checks(checks)
        second = _collapse_matrix_checks(list(reversed(checks)))
        self.assertEqual(len(first), 132)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        merged = first[0]
        self.assertEqual(merged["matrix_code"], "M-001")
        self.assertEqual(merged["occurrence_count"], 2)
        self.assertEqual({row["finding_id"] for row in merged["related_occurrences"]}, {"F1", "F2"})
        self.assertEqual(merged["evidence"], [{"file_id": "F1", "page": 1}])

    def test_pdf_rotation_raster_and_sizes_are_valid_pdfs(self):
        import fitz

        seed = rob_harness._blank_pdf(600, 800, "sample text")
        for name in ("pdf-rotate-90", "pdf-rotate-180", "pdf-rotate-270", "pdf-content-rotated", "pdf-rasterized",
                     "pdf-a0", "pdf-tiny-page", "pdf-blank-page", "pdf-long-table", "pdf-dense-vector"):
            with self.subTest(name=name):
                payload = rob_harness._pdf_variant(name, seed)
                with fitz.open(stream=payload, filetype="pdf") as pdf:
                    self.assertGreater(len(pdf), 0)

    def test_tounicode_removal_variant_has_a_null_font_map(self):
        import fitz

        try:
            payload = rob_harness._pdf_variant("pdf-no-tounicode", rob_harness._blank_pdf(text="seed"))
        except rob_harness.CaseSkipped as exc:
            self.skipTest(str(exc))
        with fitz.open(stream=payload, filetype="pdf") as pdf:
            fonts = pdf[0].get_fonts(full=True)
            self.assertTrue(fonts)
            self.assertTrue(any(pdf.xref_get_key(int(font[0]), "ToUnicode")[0] == "null" for font in fonts))

    def test_password_protected_pdf_is_marked_unreadable_in_package_scan(self):
        payload = rob_harness._pdf_variant("pdf-encrypted", rob_harness._blank_pdf(text="seed"))
        path = self.root / "password-protected.pdf"
        path.write_bytes(payload)
        self.assertIsNone(bp.pdf_page_count(path))

    def test_repaired_truncated_pdf_is_marked_low_quality(self):
        from app.domain import dataset_sources, official_evidence

        payload = rob_harness._blank_pdf(600, 800, "repaired document")
        damaged = payload[: len(payload) // 2]
        path = self.root / "repaired.pdf"
        path.write_bytes(damaged)

        scanned = bp.scan_package(self.root)
        self.assertEqual(scanned[0].processing_issue, "PDF_REPAIRED_DURING_OPEN")
        plan = bp.build_plan(scanned, None, package_name="repaired")
        self.assertEqual(plan.report["file_statuses"], [
            {"file": "repaired.pdf", "status": "LOW_QUALITY", "reason": "PDF_REPAIRED_DURING_OPEN"},
        ])

        digest = bp.sha256_file(path)
        document = SimpleNamespace(
            dataset_metadata={"document_manifest": {"relative_path": "repaired.pdf"}},
            file_hash=digest, content_hash=None,
        )
        dataset_sources._original_page_snapshots.cache_clear()
        official_evidence._QUALITY_PAGE_CACHE.clear()
        try:
            with patch.object(dataset_sources, "original_document_bytes", return_value=damaged), \
                 patch.object(dataset_sources, "_ocr_page_words", return_value=None), \
                 patch.object(official_evidence.page_quality_cache, "load", return_value=None), \
                 patch.object(official_evidence.page_quality_cache, "store"):
                snapshots = dataset_sources._original_page_snapshots("repaired.pdf", digest, (1,))
                quality = official_evidence._quality_for_page(document, 1)
            self.assertEqual(snapshots[0]["quality_status"], "LOW_QUALITY")
            self.assertIn("PDF_REPAIRED_DURING_OPEN", snapshots[0]["quality_reasons"])
            self.assertEqual(quality["quality_status"], "LOW_QUALITY")
            self.assertIn("PDF_REPAIRED_DURING_OPEN", quality["reasons"])
        finally:
            dataset_sources._original_page_snapshots.cache_clear()
            official_evidence._QUALITY_PAGE_CACHE.clear()

    def test_tesseract_timeout_is_bounded_and_returns_no_ocr_text(self):
        from PIL import Image
        from app.domain import dataset_sources

        image = Image.new("L", (16, 16), 255)
        timeout = subprocess.TimeoutExpired("tesseract", 60)
        with patch("app.domain.dataset_sources.subprocess.run", side_effect=timeout) as run:
            self.assertIsNone(dataset_sources._run_tesseract_tsv(image, "rus"))
        self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_forced_ocr_timeout_marks_only_the_page_low_quality(self):
        from app.domain import dataset_sources

        document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": "scan.pdf"}},
                                   file_hash="e" * 64, content_hash=None)
        snapshot = {"page": 3, "width": 600.0, "height": 800.0, "text": "",
                    "words": [], "cmap_corrupted": True}
        with patch("app.domain.dataset_sources._original_page_snapshots", return_value=(snapshot,)), \
             patch("app.domain.dataset_sources.ocr_page_snapshot",
                   side_effect=subprocess.TimeoutExpired("tesseract", 60)):
            result = dataset_sources.extract_original_pages(document, (3,))
        self.assertEqual(result[3]["quality_status"], "LOW_QUALITY")
        self.assertIn("BROKEN_TEXT_LAYER_OCR_UNAVAILABLE", result[3]["quality_reasons"])

    def test_page_extraction_failure_degrades_only_that_page(self):
        from app.domain import dataset_sources

        class FakePage:
            rotation = 0
            cropbox = SimpleNamespace(width=600, height=800)

            def __init__(self, number):
                self.number = number

            def get_text(self, kind, **kwargs):
                if kind == "words":
                    return []
                if self.number == 1:
                    raise RuntimeError("synthetic damaged page")
                return "readable page"

        class FakePdf:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __len__(self):
                return 2

            def __getitem__(self, index):
                return FakePage(index + 1)

        dataset_sources._original_page_snapshots.cache_clear()
        try:
            with patch("fitz.open", return_value=FakePdf()), \
                 patch("app.domain.dataset_sources.original_document_bytes", return_value=b"synthetic"):
                snapshots = dataset_sources._original_page_snapshots("synthetic.pdf", "a" * 64, (1, 2))
        finally:
            dataset_sources._original_page_snapshots.cache_clear()
        self.assertEqual(snapshots[0]["quality_status"], "LOW_QUALITY")
        self.assertIn("PAGE_EXTRACTION_FAILED", snapshots[0]["quality_reasons"])
        self.assertNotIn("quality_status", snapshots[1])

    def test_pdf_open_failure_degrades_requested_pages_to_low_quality(self):
        from app.domain import dataset_sources

        document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": "broken.pdf"}},
                                   file_hash="b" * 64, content_hash=None)
        with patch("app.domain.dataset_sources._original_page_snapshots", side_effect=RuntimeError("bad PDF")):
            result = dataset_sources.extract_original_pages(document, (1, 2))
        self.assertEqual(set(result), {1, 2})
        self.assertTrue(all(item["quality_status"] == "LOW_QUALITY" for item in result.values()))

    def test_explication_scan_reports_unreadable_pdf_without_raising(self):
        from app.domain import explication_compare

        document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": "broken.pdf"}},
                                   file_hash="c" * 64, content_hash=None)
        with patch("app.domain.explication_compare._load_cache", return_value=None), \
             patch("app.domain.dataset_sources.original_document_bytes", return_value=b"broken"), \
             patch("fitz.open", side_effect=RuntimeError("synthetic bad PDF")):
            result = explication_compare._scan_document_tables_uncached(document)
        self.assertEqual(result["skipped"], "PDF_PROCESSING_ERROR:RuntimeError")
        self.assertEqual(result["low_quality"][0]["quality_status"], "LOW_QUALITY")
        self.assertEqual(result["low_quality"][0]["reason"], "PDF_PROCESSING_ERROR")

    def test_explication_scan_skips_only_a_bad_page(self):
        from app.domain import explication_compare

        class FakePage:
            def __init__(self, number):
                self.number = number

            def get_text(self, kind):
                if self.number == 1:
                    raise RuntimeError("synthetic bad page")
                return "ordinary text"

        class FakePdf:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __len__(self):
                return 2

            def __getitem__(self, index):
                return FakePage(index + 1)

        document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": "pages.pdf"}},
                                   file_hash="d" * 64, content_hash=None)
        with patch("app.domain.explication_compare._load_cache", return_value=None), \
             patch("app.domain.dataset_sources.original_document_bytes", return_value=b"synthetic"), \
             patch("fitz.open", return_value=FakePdf()):
            result = explication_compare._scan_document_tables_uncached(document)
        self.assertEqual(result["pages_total"], 2)
        self.assertEqual(result["low_quality"], [
            {"page": 1, "quality_status": "LOW_QUALITY", "reason": "PAGE_PROCESSING_ERROR",
             "detail": "RuntimeError"},
        ])

    def test_generated_xlsx_extra_column_is_readable(self):
        import fitz

        docs = self.rooted_package()
        registry = rob_harness._write_xlsx_registry(docs, self._path("registry.xlsx"))
        rows = bp.read_registry(registry)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["doc_stage"], "PD")
        self.assertIn("extra_column", rows[0])

    def test_registry_header_value_and_section_aliases(self):
        path = self._path("tz-registry.csv")
        path.write_text(
            "Имя файла;SHA-256;Стадия документации;Код раздела;Номер изменения;Статус утверждения\n"
            "PD/sample.pdf;" + "a" * 64 + ";Проектная документация;ОВиК;1;Согласовано\n",
            encoding="utf-8-sig",
        )
        row = bp.read_registry(path)[0]
        self.assertEqual(row["file_name"], "PD/sample.pdf")
        self.assertEqual(row["sha256"], "a" * 64)
        self.assertEqual(row["doc_stage"], "PD")
        self.assertEqual(row["discipline"], "ОВиК")
        self.assertEqual(row["approval_status"], "APPROVED")
        self.assertEqual(bp._header_key("Approval Status"), "approval_status")
        self.assertEqual(bp._header_key("Имя файла"), "file_name")
        from app.domain.document_facts import canonical_manifest_section
        for alias in ("OV", "ОВ", "ОВиК", "ИОС4", "IOS4"):
            with self.subTest(alias=alias):
                self.assertIn(canonical_manifest_section(alias), {"ОВ", "ИОС4"})

    def test_tz_header_synonyms_map_document_status_and_development_stage(self):
        path = self._path("tz-synonyms.csv")
        path.write_text(
            "Файл;SHA-256;Стадия разработки;Обозначение раздела;Статус документа\n"
            "PD/sample.pdf;" + "b" * 64 + ";P;OV;Утверждён\n",
            encoding="utf-8-sig",
        )
        row = bp.read_registry(path)[0]
        self.assertEqual(row["file_name"], "PD/sample.pdf")
        self.assertEqual(row["doc_stage"], "PD")
        self.assertEqual(row["discipline"], "OV")
        self.assertEqual(row["approval_status"], "APPROVED")

    def test_absent_and_unknown_approval_are_reported_but_do_not_break_registry_matching(self):
        docs = self.rooted_package()
        for label, options in (("absent", {"missing_approval": True}),
                               ("unknown", {"approval_value": "неизвестно"})):
            with self.subTest(label=label):
                registry = rob_harness._write_json_registry(docs, self._path(f"{label}.json"), **options)
                plan = bp.build_plan(bp.scan_package(docs), bp.read_registry(registry), package_name=label,
                                     registry_sha256=bp.sha256_file(registry))
                self.assertEqual(plan.report["registry_status"], bp.STATUS_ACCEPTED)
                self.assertFalse(plan.rows[0]["registry_issues"])
                self.assertEqual(plan.report["approval_unverified_files"], ["PD/sample.pdf"])
                self.assertIn("APPROVAL_UNVERIFIED", {item["code"] for item in plan.report["registry_warnings"]})

    def test_approval_unknown_policy_remains_block_by_default(self):
        from app.robustness import approval_unknown_policy

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(approval_unknown_policy(), "block")

    def test_registry_stage_and_approval_values_have_explicit_mappings(self):
        for value in ("ПД", "Проектная документация", "P"):
            self.assertEqual(bp.normalize_stage(value), "PD")
        for value in ("РД", "Рабочая документация", "W"):
            self.assertEqual(bp.normalize_stage(value), "RD")
        for value in ("ИД", "Исполнительная документация", "A"):
            self.assertEqual(bp.normalize_stage(value), "ID")
        for value in ("Утверждён", "Согласовано", "Approved"):
            self.assertEqual(bp.normalize_approval_status(value), "APPROVED")
        self.assertEqual(bp.normalize_approval_status("Действует"), "FOR_CONSTRUCTION")
        self.assertEqual(bp.normalize_approval_status(""), "UNKNOWN")
        self.assertEqual(bp.normalize_approval_status("неизвестно"), "UNKNOWN")
        self.assertEqual(bp.normalize_approval_status("Заменён"), "SUPERSEDED")
        self.assertEqual(bp.normalize_approval_status("Отменён"), "CANCELLED")

    def test_mixed_rd_id_folder_uses_registry_stage_and_missing_registry_warns(self):
        import fitz

        docs = self.root / "mixed"
        folder = docs / "Рабочая и исполнительная документация"
        folder.mkdir(parents=True)
        pdf = fitz.open()
        pdf.new_page()
        path = folder / "sample.pdf"
        pdf.save(path)
        pdf.close()
        stage_registry = self._path("mixed.json")
        stage_registry.write_text(json.dumps([{
            "file_name": path.relative_to(docs).as_posix(), "sha256": bp.sha256_file(path), "doc_stage": "W",
        }]), encoding="utf-8")
        with_registry = bp.build_plan(bp.scan_package(docs), bp.read_registry(stage_registry), package_name="mixed",
                                      registry_sha256=bp.sha256_file(stage_registry))
        self.assertEqual(with_registry.rows[0]["stage"], "RD")
        self.assertEqual(with_registry.rows[0]["stage_source"], "REGISTRY")
        from app.domain.official_dataset import stage_from_package_path
        self.assertEqual(stage_from_package_path(path.relative_to(docs).as_posix()), "RD_ID_MIXED")
        without_registry = bp.build_plan(bp.scan_package(docs), None, package_name="mixed")
        self.assertEqual(without_registry.report["registry_status_reason"], "REGISTRY_MISSING")
        self.assertEqual(without_registry.report["unregistered_files"], [path.relative_to(docs).as_posix()])
        self.assertIn("FILE_WITHOUT_REGISTRY_ROW", {item["code"] for item in without_registry.report["registry_warnings"]})

    def test_registry_path_traversal_is_unmatched_and_warned(self):
        docs = self.rooted_package()
        registry = self._path("bad-path.json")
        registry.write_text(json.dumps([{
            "file_id": "BAD", "file_name": "../PD/sample.pdf", "doc_stage": "P",
        }]), encoding="utf-8")
        plan = bp.build_plan(bp.scan_package(docs), bp.read_registry(registry), package_name="bad-path",
                             registry_sha256=bp.sha256_file(registry))
        self.assertIn("NOT_IN_REGISTRY", plan.rows[0]["registry_issues"])
        self.assertEqual(plan.report["registry_rows_without_file"][0]["reason"], "INVALID_REGISTRY_PATH")
        self.assertEqual({item["code"] for item in plan.report["registry_warnings"]},
                         {"REGISTRY_ROW_WITHOUT_FILE", "FILE_WITHOUT_REGISTRY_ROW", "APPROVAL_UNVERIFIED",
                          "INVALID_REGISTRY_PATH"})

    def test_unreadable_directory_is_reported_instead_of_silent_partial_scan(self):
        docs = self.root / "unreadable-directory"
        docs.mkdir()
        (docs / "good.pdf").write_bytes(b"readable")
        errors: list[dict[str, str]] = []

        def walk(_root, onerror=None):
            yield str(docs), [], ["good.pdf"]
            if onerror:
                error = PermissionError("synthetic directory denial")
                error.filename = str(docs / ("а" * 154))
                onerror(error)

        with patch("app.batch_package.os.walk", side_effect=walk):
            files = bp.scan_package(docs, scan_errors=errors)
        plan = bp.build_plan(files, None, package_name="unreadable-directory", scan_errors=errors)
        self.assertEqual(len(files), 1)
        self.assertEqual(plan.report["scan_errors"][0]["reason"], "DIRECTORY_READ_ERROR")
        self.assertIn("DIRECTORY_READ_ERROR", {item["reason"] for item in plan.report["scan_errors"]})

    def test_one_file_metadata_error_does_not_abort_sibling_scan(self):
        from pathlib import Path

        docs = self.root / "file-metadata-error"
        docs.mkdir()
        (docs / "good.pdf").write_bytes(b"good")
        (docs / "unreadable.pdf").write_bytes(b"bad")
        errors: list[dict[str, str]] = []
        original_is_file = Path.is_file

        def is_file(path):
            if path.name == "unreadable.pdf":
                raise PermissionError("synthetic metadata denial")
            return original_is_file(path)

        with patch.object(Path, "is_file", is_file):
            files = bp.scan_package(docs, scan_errors=errors)
        self.assertEqual([item.relative_path for item in files], ["good.pdf"])
        self.assertEqual(errors, [{"file": "unreadable.pdf", "reason": "FILE_METADATA_ERROR"}])
        plan = bp.build_plan(files, None, package_name="file-metadata-error", scan_errors=errors)
        self.assertEqual(plan.report["scan_errors"], errors)

    def test_registry_missing_required_columns_is_clarification_required(self):
        docs = self.rooted_package()
        registry = rob_harness._write_json_registry(docs, self._path("incomplete.json"), missing_columns=True)
        plan = bp.build_plan(bp.scan_package(docs), bp.read_registry(registry), package_name="test",
                             registry_sha256=bp.sha256_file(registry))
        self.assertEqual(plan.report["registry_status"], "CLARIFICATION_REQUIRED")
        self.assertEqual(plan.report["registry_status_reason"], "REGISTRY_INCOMPLETE")
        self.assertIn("REGISTRY_MISSING_SHA256", plan.rows[0]["registry_issues"])
        self.assertIn("REGISTRY_MISSING_STAGE", plan.rows[0]["registry_issues"])

    def test_empty_registry_is_clarification_required(self):
        docs = self.rooted_package()
        plan = bp.build_plan(bp.scan_package(docs), [], package_name="test", registry_sha256="f" * 64)
        self.assertEqual(plan.report["registry_status"], "CLARIFICATION_REQUIRED")
        self.assertEqual(plan.report["registry_status_reason"], "REGISTRY_EMPTY")

    def test_duplicate_registry_row_is_clarification_required(self):
        docs = self.rooted_package()
        registry = rob_harness._write_json_registry(docs, self._path("duplicate.json"), duplicate_first=True)
        plan = bp.build_plan(bp.scan_package(docs), bp.read_registry(registry), package_name="test",
                             registry_sha256=bp.sha256_file(registry))
        self.assertEqual(plan.report["registry_status"], "CLARIFICATION_REQUIRED")
        self.assertEqual(len(plan.report["registry_rows_without_file"]), 1)

    def test_creation_order_variants_have_identical_package_bytes_and_registry(self):
        sample = rob_harness._blank_pdf(text="sample")
        forward = rob_harness.build_case("package-order-forward", self.root / "forward", sample)
        reverse = rob_harness.build_case("package-order-reverse", self.root / "reverse", sample)
        files_forward = {p.relative_to(forward.docs).as_posix(): p.read_bytes() for p in forward.docs.rglob("*") if p.is_file()}
        files_reverse = {p.relative_to(reverse.docs).as_posix(): p.read_bytes() for p in reverse.docs.rglob("*") if p.is_file()}
        self.assertEqual(files_forward, files_reverse)
        self.assertEqual(forward.registry.read_bytes(), reverse.registry.read_bytes())

    def test_long_filename_variant_exceeds_200_utf8_bytes(self):
        case = rob_harness.build_case("file-name-over-200-bytes", self.root / "long-name",
                                      rob_harness._blank_pdf(text="seed"))
        long_name = next(path.name for path in case.docs.rglob("*.pdf") if len(path.name.encode("utf-8")) > 200)
        self.assertEqual(len(long_name.encode("utf-8")), 201)

    def test_154_cyrillic_directory_matches_linux_name_max_case(self):
        case = rob_harness.build_case("file-dir-name-154-cyrillic", self.root / "long-directory",
                                      rob_harness._blank_pdf(text="seed"))
        directory = next(path.parent for path in case.docs.rglob("sample.pdf"))
        self.assertEqual(len(directory.name), 154)
        self.assertGreater(len(directory.name.encode("utf-8")), 255)
        self.assertLess(len(directory.name.encode("utf-16-le")) // 2, 255)

    def test_renamed_stage_folders_use_registry_stage(self):
        sample = rob_harness._blank_pdf(text="sample")
        case = rob_harness.build_case("package-renamed-stages", self.root / "renamed", sample)
        plan = bp.build_plan(bp.scan_package(case.docs), bp.read_registry(case.registry), package_name="test",
                             registry_sha256=bp.sha256_file(case.registry))
        stages = {row["package_path"].split("/")[0]: row["stage"] for row in plan.rows}
        self.assertEqual(stages, {"design_stage": "PD", "work_stage": "RD", "as_built_stage": "ID"})
        self.assertTrue(all(row["stage_source"] == "REGISTRY" for row in plan.rows))

    def test_unicode_normalization_collisions_have_a_stable_path_tiebreak(self):
        import unicodedata

        docs = self.root / "unicode-names"
        folder = docs / "PD"
        folder.mkdir(parents=True)
        names = ["café.pdf", unicodedata.normalize("NFD", "café.pdf")]
        for index, name in enumerate(reversed(names)):
            (folder / name).write_bytes(f"pdf-{index}".encode())
        actual = [item.relative_path for item in bp.scan_package(docs)]
        expected = sorted(actual, key=lambda relative: (bp._path_sort_key(relative), relative))
        self.assertEqual(actual, expected)

    def test_zip_slip_path_is_rejected(self):
        self.assertIsNone(bp._safe_member_path("../../outside.pdf"))
        self.assertIsNone(bp._safe_member_path("C:/outside.pdf"))
        self.assertEqual(bp._safe_member_path("PD/inside.pdf"), "PD/inside.pdf")

    def test_zip_exclusion_sidecar_is_not_applied_to_a_plain_folder(self):
        package = self.root / "ordinary"
        package.mkdir()
        (self.root / ".ordinary.exclusions.json").write_text(
            '[{"file":"old.pdf","reason":"PARENT_TRAVERSAL"}]', encoding="utf-8")
        self.assertEqual(bp.package_exclusions(package), [])

    def test_zip_slip_is_reported_as_an_excluded_file(self):
        archive = self.root / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("PD/safe.pdf", b"safe")
            package.writestr("../../outside.pdf", b"unsafe")
            package.writestr("../outside_dir/", b"")
        unpacked, _ = bp.resolve_package(archive, unpack_root=self.root / "unpacked")
        self.assertEqual(bp.package_exclusions(unpacked), [
            {"file": "../../outside.pdf", "reason": "PARENT_TRAVERSAL"},
            {"file": "../outside_dir/", "reason": "PARENT_TRAVERSAL"},
        ])

    def test_zip_central_directory_failure_is_a_package_error(self):
        archive = self.root / "malformed.zip"
        archive.write_bytes(b"synthetic ZIP header")
        with patch("app.batch_package.zipfile.ZipFile", side_effect=zipfile.BadZipFile("bad central directory")):
            with self.assertRaisesRegex(bp.PackageError, "cannot read ZIP package.*BadZipFile"):
                bp.unpack_zip(archive, self.root / "unpacked")

    def test_corrupt_zip_member_is_excluded_while_survivor_is_extracted(self):
        case = rob_harness.build_case("zip-corrupt-member", self.root / "mutated",
                                      rob_harness._blank_pdf(text="seed"))
        unpacked = bp.unpack_zip(case.docs, self.root / "unpacked")
        self.assertTrue((unpacked / "package" / "PD" / "safe.pdf").is_file())
        self.assertFalse((unpacked / "package" / "PD" / "bad.pdf").exists())
        self.assertEqual(bp.package_exclusions(unpacked), [
            {"file": "package/PD/bad.pdf", "reason": "CORRUPT_ZIP_MEMBER"},
        ])

    def test_nested_zip_is_retained_as_an_explicitly_excluded_file(self):
        case = rob_harness.build_case("zip-nested", self.root / "nested", rob_harness._blank_pdf(text="seed"))
        unpacked = bp.unpack_zip(case.docs, self.root / "unpacked")
        package = unpacked / "package"
        self.assertTrue((package / "PD" / "nested.zip").is_file())
        plan = bp.build_plan(bp.scan_package(package), bp.read_registry(case.registry), package_name="nested",
                             registry_sha256=bp.sha256_file(case.registry))
        self.assertEqual(plan.report["excluded_files"], [
            {"file": "PD/nested.zip", "reason": "UNSUPPORTED_EXTENSION"},
        ])

    def test_incomplete_zip_extraction_is_cleaned_before_retry(self):
        archive = self.root / "retry.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("PD/safe.pdf", b"safe")
        unpacked = bp.unpack_zip(archive, self.root / "unpacked")
        (unpacked / ".unpacked").unlink()
        (unpacked / "stale.txt").write_text("stale", encoding="utf-8")
        retried = bp.unpack_zip(archive, self.root / "unpacked")
        self.assertFalse((retried / "stale.txt").exists())
        self.assertTrue((retried / "PD" / "safe.pdf").is_file())

    def test_zip_path_conflict_does_not_discard_other_members(self):
        archive = self.root / "conflict.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("a", b"file blocks directory")
            package.writestr("a/b.pdf", b"conflicting member")
            package.writestr("PD/safe.pdf", b"survivor")
        unpacked = bp.unpack_zip(archive, self.root / "unpacked")
        self.assertTrue((unpacked / "PD" / "safe.pdf").is_file())
        self.assertEqual(bp.package_exclusions(unpacked), [
            {"file": "a/b.pdf", "reason": "PATH_CONFLICT"},
        ])

    def test_unsupported_extension_is_excluded_with_reason(self):
        import fitz

        docs = self.root / "extensions"
        docs.mkdir()
        pdf = fitz.open()
        pdf.new_page()
        pdf.save(docs / "supported.pdf")
        (docs / "unsupported.xyz").write_bytes(b"payload")
        files = bp.scan_package(docs)
        plan = bp.build_plan(files, None, package_name="test")
        self.assertEqual(plan.report["files_total"], 2)
        self.assertEqual(plan.report["files_imported"], 1)
        self.assertEqual(next(item.sha256 for item in files if item.relative_path == "unsupported.xyz"), "")
        self.assertEqual(plan.report["excluded_files"], [
            {"file": "unsupported.xyz", "reason": "UNSUPPORTED_EXTENSION"},
        ])

    def test_unreadable_pdf_has_processing_error_status(self):
        docs = self.root / "bad-pdf"
        docs.mkdir()
        (docs / "broken.pdf").write_bytes(b"%PDF-1.7\ninvalid")
        plan = bp.build_plan(bp.scan_package(docs), None, package_name="bad-pdf")
        self.assertEqual(plan.report["pdf_unreadable"], ["broken.pdf"])
        self.assertEqual(plan.report["file_statuses"], [
            {"file": "broken.pdf", "status": "PROCESSING_ERROR", "reason": "PDF_UNREADABLE"},
        ])

    def test_unreadable_file_is_excluded_without_aborting_scan(self):
        docs = self.root / "read-errors"
        docs.mkdir()
        (docs / "good.pdf").write_bytes(b"readable bytes")
        (docs / "bad.pdf").write_bytes(b"unreadable bytes")
        original_hash = bp.sha256_file

        def selective_hash(path):
            if Path(path).name == "bad.pdf":
                raise PermissionError("synthetic unreadable file")
            return original_hash(path)

        with patch("app.batch_package.sha256_file", side_effect=selective_hash):
            files = bp.scan_package(docs)
        plan = bp.build_plan(files, None, package_name="read-errors")
        self.assertEqual(plan.report["files_total"], 2)
        self.assertEqual(plan.report["files_imported"], 1)
        self.assertEqual(plan.report["excluded_files"], [
            {"file": "bad.pdf", "reason": "FILE_READ_ERROR"},
        ])

    def rooted_package(self) -> Path:
        import fitz

        docs = self.root / "xlsx-docs"
        path = docs / "PD" / "sample.pdf"
        path.parent.mkdir(parents=True)
        doc = fitz.open()
        doc.new_page()
        doc.save(path)
        return docs

    def _path(self, name: str) -> Path:
        return self.root / name

    def setUp(self):
        self.old_robust = os.environ.get("CASE10_ROBUST_INPUTS")
        os.environ["CASE10_ROBUST_INPUTS"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        if self.old_robust is None:
            os.environ.pop("CASE10_ROBUST_INPUTS", None)
        else:
            os.environ["CASE10_ROBUST_INPUTS"] = self.old_robust


if __name__ == "__main__":
    unittest.main()
