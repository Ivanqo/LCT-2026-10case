"""Determinism / parallelism / cache / coverage tests for the live candidate
tagger (`live_candidate_tagger.py`, `live_tagger_scan.py`).

The contract under test: a tagging run's output is a pure function of the
object's documents -- never of input order, worker count, scheduling, cache
state or machine speed. Two layers:

* plan-level tests mock `extract_original_pages` (in-process, no PDFs);
* pool/cache tests write real synthetic PDFs to a temp directory, point
  `CASE10_ORIGINALS_ROOT` at it and run REAL spawned worker processes, then
  compare the resulting `SourceFragment` tables byte for byte.

Synthetic PDFs use Latin parameter names: the anchor matcher is
language-agnostic and PyMuPDF's built-in fonts carry no Cyrillic glyphs."""
from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.models import Base, DocumentVersion, SourceFragment
from app.domain import live_candidate_tagger as tagger
from app.domain import live_tagger_scan as scan
from app.domain.live_candidate_tagger import live_tagger_coverage, tag_live_candidates

_NO_SEMANTIC = patch("app.domain.live_candidate_tagger.semantic_text_similarity", return_value=None)


def _param(code: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        code=code, parameter_name=name, source_pd=True, source_rd=True, source_id=True, other_normative=None,
    )


PARAMS = [
    _param("PZ-001", "Total building area"),
    _param("PZ-002", "Construction volume"),
    _param("PZ-003", "Number of floors"),
]


def _row_words(rows: list[list[str]]) -> list[dict]:
    words, y = [], 100.0
    for row in rows:
        x = 40.0
        for cell in row:
            width = max(70.0, len(cell) * 7.0)
            words.append({"text": cell, "bbox": [x, y, x + width, y + 10.0]})
            x += width + 20.0
        y += 14.0
    return words


def _snapshot(rows: list[list[str]]) -> dict[int, dict]:
    return {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words(rows)}}


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add_doc(db, *, file_id: str, stage="RD", relative=None, pages=1, sha=None) -> DocumentVersion:
    doc = DocumentVersion(
        project_id=1, organization_id=1, source_type="case10_dataset", filename=f"{file_id}.pdf",
        dataset_file_id=file_id, dataset_stage=stage, discipline="АР",
        dataset_metadata={"document_manifest": {"pdf_pages": pages, "relative_path": relative or f"{file_id}.pdf"}},
        file_hash=sha or f"sha-{file_id}",
    )
    db.add(doc)
    db.flush()
    return doc


def _fragment_table(db) -> list[tuple]:
    """Everything a downstream consumer could observe about the written rows,
    in row-id order (ids themselves are not compared: they are DB-assigned,
    but ORDER of creation is part of the contract)."""
    rows = db.query(SourceFragment).order_by(SourceFragment.id).all()
    files = {d.id: d.dataset_file_id for d in db.query(DocumentVersion).all()}
    return [
        (files[r.document_version_id], r.external_id, r.page, r.text, json.dumps(r.bbox_pdf), r.page_width,
         r.page_height, r.confidence, json.dumps(r.metadata_json, sort_keys=True))
        for r in rows
    ]


def _sha(table: list[tuple]) -> str:
    return hashlib.sha256(json.dumps(table, ensure_ascii=False).encode()).hexdigest()


class WallClockIsGoneTests(unittest.TestCase):
    def test_budget_is_documents_and_pages_only(self):
        self.assertEqual(set(tagger.new_live_tagger_budget()), {"documents", "pages"})

    def test_no_wall_clock_setting_exists(self):
        self.assertFalse(hasattr(settings, "LIVE_TAGGER_MAX_SECONDS"))

    def test_default_caps_cover_the_largest_known_object(self):
        # DOO25: 1750 files, 1294 PDFs, ~14.9k pages after the per-document cap.
        self.assertGreaterEqual(settings.LIVE_TAGGER_MAX_DOCUMENTS, 1750)
        self.assertGreaterEqual(settings.LIVE_TAGGER_MAX_PAGES_TOTAL, 14890)


class PlanDeterminismTests(unittest.TestCase):
    def setUp(self):
        self.db = _session()

    def tearDown(self):
        self.db.close()

    def _snapshots(self):
        return {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([["Total", "building", "area", "10,5"]])}}

    def test_cap_selects_the_same_documents_whatever_the_input_order(self):
        selections = []
        for seed in (1, 2, 3):
            db = _session()
            docs = [_add_doc(db, file_id=f"F{i:03d}") for i in range(8)]
            random.Random(seed).shuffle(docs)
            with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=self._snapshots()), _NO_SEMANTIC:
                diagnostics = tag_live_candidates(db, docs, PARAMS, budget={"documents": 3.0, "pages": 100.0})
            scanned = sorted(d.dataset_file_id for d in docs if tagger._already_scanned(d))
            selections.append(scanned)
            self.assertEqual(diagnostics["documents_planned"], 3)
            self.assertEqual(diagnostics["documents_deferred_by_cap"], 5)
            db.close()
        self.assertEqual(selections[0], selections[1])
        self.assertEqual(selections[1], selections[2])
        # Total order: identical priority is broken by dataset_file_id.
        self.assertEqual(selections[0], ["F000", "F001", "F002"])

    def test_fragment_rows_are_created_in_a_fixed_order(self):
        tables = []
        for seed in (11, 12):
            db = _session()
            docs = [_add_doc(db, file_id=f"F{i:03d}") for i in range(6)]
            random.Random(seed).shuffle(docs)
            with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=self._snapshots()), _NO_SEMANTIC:
                tag_live_candidates(db, docs, PARAMS)
            tables.append(_fragment_table(db))
            db.close()
        self.assertEqual(tables[0], tables[1])
        self.assertEqual([row[0] for row in tables[0]], sorted(row[0] for row in tables[0]))

    def test_anchor_order_does_not_depend_on_catalog_query_order(self):
        tables = []
        for params in (PARAMS, list(reversed(PARAMS))):
            db = _session()
            doc = _add_doc(db, file_id="F001")
            snap = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([
                ["Total", "building", "area", "10,5"], ["Construction", "volume", "20"], ["Number", "of", "floors", "9"],
            ])}}
            with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snap), _NO_SEMANTIC:
                tag_live_candidates(db, [doc], params)
            tables.append(_fragment_table(db))
            db.close()
        self.assertEqual(tables[0], tables[1])
        self.assertEqual(len(tables[0]), 3)

    def test_page_cap_per_document_and_total_are_applied_in_plan_order(self):
        docs = [_add_doc(db=self.db, file_id=f"F{i}", pages=50) for i in range(3)]
        with patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_MAX_PAGES_PER_DOCUMENT", 20), \
             patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=self._snapshots()) as mocked, _NO_SEMANTIC:
            diagnostics = tag_live_candidates(self.db, docs, PARAMS, budget={"documents": 10.0, "pages": 30.0})
        requested = [len(call.args[1]) for call in mocked.call_args_list]
        self.assertEqual(requested, [20, 10])  # 20 (per-document cap), then the last 10 of the total
        self.assertEqual(diagnostics["documents_planned"], 2)
        self.assertEqual(diagnostics["documents_deferred_by_cap"], 1)
        # Both were cut short of the 50 pages the manifest promised.
        self.assertEqual(sorted(tagger.scan_marker(d)["status"] for d in docs if tagger.scan_marker(d)), ["partial", "partial"])

    def test_rerun_is_charged_for_documents_already_scanned(self):
        docs = [_add_doc(self.db, file_id=f"F{i}") for i in range(4)]
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=self._snapshots()), _NO_SEMANTIC:
            first = tag_live_candidates(self.db, docs, PARAMS, budget={"documents": 2.0, "pages": 100.0})
            second = tag_live_candidates(self.db, docs, PARAMS, budget={"documents": 2.0, "pages": 100.0})
        self.assertEqual(first["documents_scanned"], 2)
        # A re-run converges on the same set instead of walking on down the list.
        self.assertEqual(second["documents_scanned"], 0)
        self.assertEqual(second["documents_skipped_already_tagged"], 2)
        self.assertEqual(sum(1 for d in docs if tagger._already_scanned(d)), 2)

    def test_non_pdf_documents_take_no_budget(self):
        rar = _add_doc(self.db, file_id="A-rar", relative="ID/archive.rar")
        docx = _add_doc(self.db, file_id="B-docx", relative="ID/act.docx")
        pdf = _add_doc(self.db, file_id="C-pdf")
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=self._snapshots()) as mocked, _NO_SEMANTIC:
            diagnostics = tag_live_candidates(self.db, [rar, docx, pdf], PARAMS, budget={"documents": 1.0, "pages": 100.0})
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(diagnostics["documents_scanned"], 1)
        self.assertEqual(diagnostics["documents_not_scannable"], 2)
        self.assertEqual(diagnostics["documents_deferred_by_cap"], 0)

    def test_marker_carries_no_timestamp(self):
        doc = _add_doc(self.db, file_id="F1")
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=self._snapshots()), _NO_SEMANTIC:
            tag_live_candidates(self.db, [doc], PARAMS)
        marker = tagger.scan_marker(doc)
        self.assertEqual(marker["status"], "scanned")
        self.assertNotIn("live_tagger_scanned_at", doc.dataset_metadata)
        self.assertNotIn("20", json.dumps(marker).replace("pages_total", ""))  # no ISO year sneaks in

    def test_legacy_timestamp_marker_is_still_honoured(self):
        doc = _add_doc(self.db, file_id="F1")
        doc.dataset_metadata = {**doc.dataset_metadata, "live_tagger_scanned_at": "2026-09-01T00:00:00+00:00"}
        with patch("app.domain.live_candidate_tagger.extract_original_pages") as mocked:
            diagnostics = tag_live_candidates(self.db, [doc], PARAMS)
        mocked.assert_not_called()
        self.assertEqual(diagnostics["documents_skipped_already_tagged"], 1)

    def test_failed_document_is_recorded_and_retried_next_run(self):
        doc = _add_doc(self.db, file_id="F1")
        with patch("app.domain.live_candidate_tagger.extract_original_pages", side_effect=ValueError("bad pdf")):
            diagnostics = tag_live_candidates(self.db, [doc], PARAMS)
        self.assertEqual(diagnostics["documents_failed"], 1)
        self.assertEqual(tagger.scan_marker(doc)["status"], "failed")
        self.assertFalse(tagger._already_scanned(doc))
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=self._snapshots()), _NO_SEMANTIC:
            again = tag_live_candidates(self.db, [doc], PARAMS)
        self.assertEqual(again["documents_scanned"], 1)


class CoverageReportTests(unittest.TestCase):
    def setUp(self):
        self.db = _session()

    def tearDown(self):
        self.db.close()

    def test_coverage_counts_per_stage_and_says_what_was_left_out(self):
        rd = [_add_doc(self.db, file_id=f"R{i}", stage="RD", pages=5) for i in range(4)]
        pd = [_add_doc(self.db, file_id="P0", stage="PD", pages=300)]
        docx = _add_doc(self.db, file_id="R-docx", stage="RD", relative="x.docx")
        mixed = _add_doc(self.db, file_id="M0", stage="RD_ID_MIXED")
        blank = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": []}}
        with patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_MAX_PAGES_PER_DOCUMENT", 220), \
             patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=blank):
            tag_live_candidates(self.db, rd + pd + [docx, mixed], PARAMS, budget={"documents": 4.0, "pages": 1000.0})
        coverage = live_tagger_coverage(rd + pd + [docx, mixed])
        row_rd, row_pd = coverage["stages"]["RD"], coverage["stages"]["PD"]
        self.assertEqual(row_rd["documents_total"], 5)
        self.assertEqual(row_rd["documents_not_scannable"], 1)
        self.assertEqual(row_rd["documents_scannable"], 4)
        # Budget of 4 documents: PD (300 pages, 220 scanned) ranks after the cheaper RD ones or not -- but
        # exactly 4 of the 5 PDFs got scanned, the fifth is reported, not dropped.
        self.assertEqual(row_rd["documents_scanned"] + row_pd["documents_scanned"], 4)
        self.assertEqual(row_rd["documents_not_scanned"] + row_pd["documents_not_scanned"], 1)
        self.assertEqual(row_rd["documents_no_text_layer"] + row_pd["documents_no_text_layer"], 4)
        self.assertEqual(coverage["documents_other_stage"], 1)
        self.assertFalse(coverage["complete"])
        self.assertIn("просканировано", coverage["summary"])
        self.assertIn("не просканировано из-за лимита", coverage["summary"])

    def test_complete_when_every_scannable_document_was_scanned(self):
        docs = [_add_doc(self.db, file_id=f"R{i}", pages=2) for i in range(3)]
        snap = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([["x", "y"]])}}
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snap):
            tag_live_candidates(self.db, docs, PARAMS)
        coverage = live_tagger_coverage(docs)
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["stages"]["RD"]["documents_scanned"], 3)
        self.assertIn("просканировано 3 из 3 документов", coverage["summary"])

    def test_disabled_tagger_says_so(self):
        docs = [_add_doc(self.db, file_id="R0")]
        with patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_ENABLED", False):
            coverage = live_tagger_coverage(docs)
        self.assertFalse(coverage["enabled"])
        self.assertFalse(coverage["complete"])
        self.assertIn("отключён", coverage["summary"])


class CpuCountTests(unittest.TestCase):
    def test_affinity_and_cgroup_quota_bound_the_worker_pool(self):
        with patch("app.domain.live_tagger_scan.os.sched_getaffinity", create=True, return_value=set(range(48))), \
             patch("app.domain.live_tagger_scan._cgroup_cpu_limit", return_value=None):
            self.assertEqual(scan.available_cpu_count(), 48)
        with patch("app.domain.live_tagger_scan.os.sched_getaffinity", create=True, return_value=set(range(48))), \
             patch("app.domain.live_tagger_scan._cgroup_cpu_limit", return_value=6.5):
            self.assertEqual(scan.available_cpu_count(), 6)
        with patch("app.domain.live_tagger_scan.os.sched_getaffinity", create=True, side_effect=OSError), \
             patch("app.domain.live_tagger_scan.os.cpu_count", return_value=12), \
             patch("app.domain.live_tagger_scan._cgroup_cpu_limit", return_value=0.2):
            self.assertEqual(scan.available_cpu_count(), 1)

    def test_auto_worker_count_needs_real_work_per_worker(self):
        item = lambda pages: SimpleNamespace(pages=list(range(pages)))  # noqa: E731
        with patch("app.domain.live_candidate_tagger._in_process_required", return_value=False), \
             patch("app.domain.live_candidate_tagger.available_cpu_count", return_value=24), \
             patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_WORKERS", 0):
            self.assertEqual(tagger._worker_count([item(20)] * 5), 1)            # 100 pages: not worth a pool
            self.assertEqual(tagger._worker_count([item(100)] * 6), 6)           # 600 pages -> 6 workers
            self.assertEqual(tagger._worker_count([item(200)] * 300), 24)        # capped by CPUs
        with patch("app.domain.live_candidate_tagger._in_process_required", return_value=False), \
             patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_WORKERS", 3):
            self.assertEqual(tagger._worker_count([item(1)] * 8), 3)             # explicit value is used as-is
        with patch("app.domain.live_candidate_tagger._in_process_required", return_value=True):
            self.assertEqual(tagger._worker_count([item(500)] * 8), 1)

    def test_a_worker_process_never_starts_a_nested_pool(self):
        item = SimpleNamespace(pages=list(range(500)))
        with patch("app.domain.live_candidate_tagger._in_process_required", return_value=False),              patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_WORKERS", 4),              patch("app.domain.live_candidate_tagger.multiprocessing.parent_process", return_value=object()),              self.assertLogs("app.domain.live_candidate_tagger", level="WARNING") as logs:
            self.assertEqual(tagger._worker_count([item] * 8), 1)
        self.assertIn("__main__ guard", logs.output[0])


def _write_pdf(path: Path, pages: list[list[str]]) -> str:
    import fitz

    pdf = fitz.open()
    for lines in pages:
        page = pdf.new_page(width=595, height=842)
        y = 80
        for line in lines:
            page.insert_text((60, y), line, fontname="helv", fontsize=11)
            y += 18
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(str(path))
    pdf.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RealPdfPoolAndCacheTests(unittest.TestCase):
    """Real PDFs on disk, REAL spawned worker processes."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "originals"
        cls.cache = Path(cls._tmp.name) / "cache"
        cls.docs: list[tuple[str, str, str, int]] = []  # (file_id, relative, sha, pages)
        for i in range(6):
            pages = []
            for p in range(3):
                lines = [f"Sheet {i}-{p}", "Some unrelated drawing text"]
                if (i + p) % 2 == 0:
                    lines += [f"Total building area {1000 + i * 10 + p},5 m2"]
                if (i * p) % 3 == 1:
                    lines += [f"Construction volume {50 + i},25 m3", f"Number of floors {i + 2}"]
                pages.append(lines)
            relative = f"RD/doc{i}.pdf"
            cls.docs.append((f"OBJ-{i:06d}", relative, _write_pdf(cls.root / relative, pages), 3))
        # A byte-identical copy under another name -> shares one scan.
        dup_rel = "RD/copy_of_doc0.pdf"
        (cls.root / dup_rel).write_bytes((cls.root / cls.docs[0][1]).read_bytes())
        cls.docs.append(("OBJ-000099", dup_rel, cls.docs[0][2], 3))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run(self, *, workers: int, cache: bool, order_seed: int = 0):
        db = _session()
        docs = [_add_doc(db, file_id=fid, relative=rel, pages=n, sha=sha) for fid, rel, sha, n in self.docs]
        random.Random(order_seed).shuffle(docs)
        env = {"CASE10_ORIGINALS_ROOT": str(self.root)}
        with patch.dict(os.environ, env), \
             patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_WORKERS", workers), \
             patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_CACHE_ENABLED", cache), \
             patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_CACHE_DIR", self.cache), \
             _NO_SEMANTIC:
            # The REAL page source, un-mocked: this is the path worker processes take.
            diagnostics = tag_live_candidates(db, docs, PARAMS)
        table = _fragment_table(db)
        db.close()
        return diagnostics, table

    def test_pool_output_is_byte_identical_to_in_process_output(self):
        sequential_diag, sequential = self._run(workers=1, cache=False)
        # A silent fallback to in-process would also give the same answer, so
        # prove the pool really ran: no "pool failed"/"workers failed" warning
        # and no document had to be re-scanned in the parent.
        with self.assertNoLogs("app.domain.live_candidate_tagger", level="WARNING"):
            pooled_diag, pooled = self._run(workers=3, cache=False, order_seed=5)
        self.assertEqual(sequential_diag["workers"], 1)
        self.assertEqual(pooled_diag["workers"], 3)
        self.assertEqual(pooled_diag["worker_failures_retried"], 0)
        self.assertGreater(len(sequential), 6)
        self.assertEqual(_sha(sequential), _sha(pooled))
        self.assertEqual(sequential_diag["fragments_created"], pooled_diag["fragments_created"])

    def test_fragments_match_what_is_on_the_pages(self):
        _diag, table = self._run(workers=1, cache=False)
        by_doc = {}
        for file_id, external_id, page, text, *_rest in table:
            by_doc.setdefault(file_id, []).append((page, external_id.split(":")[-1], text))
        # doc0 page 1 is (0+0)%2==0 -> has "Total building area 1000,5"
        self.assertIn((1, "PZ-001", "Total building area 1000,5 m2"), by_doc["OBJ-000000"])
        self.assertTrue(all(isinstance(row[3], str) and row[3] for row in table))

    def test_cache_makes_the_second_run_instant_and_identical(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(type(self), "cache", Path(tmp)):
            cold_diag, cold = self._run(workers=2, cache=True)
            self.assertEqual(cold_diag["cache_hits"], 0)
            self.assertGreater(len(list(Path(tmp).rglob("*.json"))), 0)
            # Any attempt to read a PDF now is a bug: everything must come from the cache.
            with patch("app.domain.dataset_sources._original_page_snapshots", side_effect=AssertionError("cache miss")), \
                 patch("app.domain.dataset_sources._original_document_bytes", side_effect=AssertionError("cache miss")):
                warm_diag, warm = self._run(workers=2, cache=True, order_seed=9)
            self.assertEqual(warm_diag["cache_hits"], len(self.docs))
            self.assertEqual(warm_diag["workers"], 0)
            self.assertEqual(_sha(cold), _sha(warm))

    def test_measurement_cpu_time_never_reaches_the_cache_or_the_merge(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(type(self), "cache", Path(tmp)):
            diag, _table = self._run(workers=2, cache=True)
            self.assertGreater(diag["scan_cpu_seconds"], 0.0)
            for path in Path(tmp).rglob("*.json"):
                self.assertNotIn("_cpu_seconds", path.read_text(encoding="utf-8"))

    def test_identical_content_is_scanned_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(type(self), "cache", Path(tmp)):
            self._run(workers=1, cache=True)
            files = list(Path(tmp).rglob("*.json"))
        # 7 documents, but the copy shares doc0's scan (same SHA-256, stage, anchors, pages).
        self.assertEqual(len(files), 6)

    def test_corrupt_or_foreign_cache_file_is_a_miss_not_a_result(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(type(self), "cache", Path(tmp)):
            self._run(workers=1, cache=True)
            for path in Path(tmp).rglob("*.json"):
                path.write_text("{ truncated", encoding="utf-8")
            diag, table = self._run(workers=1, cache=True)
            self.assertEqual(diag["cache_hits"], 0)
            self.assertGreater(len(table), 6)

    def test_cache_key_reacts_to_everything_that_changes_the_answer(self):
        anchors = [("PZ-001", "Total building area")]
        base = scan.cache_key("abc", "RD", anchors, 3, {"ocr_engine": "tesseract"})
        self.assertEqual(base, scan.cache_key("abc", "RD", list(anchors), 3, {"ocr_engine": "tesseract"}))
        self.assertNotEqual(base, scan.cache_key("abd", "RD", anchors, 3, {"ocr_engine": "tesseract"}))
        self.assertNotEqual(base, scan.cache_key("abc", "PD", anchors, 3, {"ocr_engine": "tesseract"}))
        self.assertNotEqual(base, scan.cache_key("abc", "RD", anchors, 4, {"ocr_engine": "tesseract"}))
        self.assertNotEqual(base, scan.cache_key("abc", "RD", anchors + [("PZ-002", "Construction volume")], 3, {"ocr_engine": "tesseract"}))
        self.assertNotEqual(base, scan.cache_key("abc", "RD", anchors, 3, {"ocr_engine": "paddleocr"}))
        with patch("app.domain.live_tagger_scan.scan_code_fingerprint", return_value="another-code-version"):
            self.assertNotEqual(base, scan.cache_key("abc", "RD", anchors, 3, {"ocr_engine": "tesseract"}))

    def test_a_broken_pool_falls_back_to_in_process_with_the_same_answer(self):
        _d, expected = self._run(workers=1, cache=False)
        with patch("app.domain.live_candidate_tagger.ProcessPoolExecutor", side_effect=OSError("no fork for you")):
            diag, table = self._run(workers=3, cache=False)
        self.assertEqual(diag["workers"], 3)  # it tried
        self.assertEqual(_sha(expected), _sha(table))

    def test_unreadable_document_fails_alone(self):
        db = _session()
        good = _add_doc(db, file_id="G", relative=self.docs[0][1], pages=3, sha=self.docs[0][2])
        bad = _add_doc(db, file_id="B", relative="RD/missing.pdf", pages=3, sha="0" * 64)
        with patch.dict(os.environ, {"CASE10_ORIGINALS_ROOT": str(self.root)}), \
             patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_WORKERS", 2), \
             patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_CACHE_ENABLED", False), _NO_SEMANTIC:
            diag = tag_live_candidates(db, [good, bad], PARAMS)
        self.assertEqual(diag["documents_scanned"], 1)
        self.assertEqual(diag["documents_failed"], 1)
        self.assertEqual(tagger.scan_marker(bad)["status"], "failed")
        self.assertEqual(tagger.scan_marker(good)["status"], "scanned")
        db.close()


if __name__ == "__main__":
    unittest.main()
