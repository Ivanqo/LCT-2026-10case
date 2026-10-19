"""PERF2: stat-keyed file digest cache, opt-in JSON gzip, and the cheap path of the image-stamp quality check."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from app.domain import file_hash_cache
from app.json_gzip import LargeJsonGzipMiddleware


def _digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FileHashCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env = mock.patch.dict(os.environ, {"CASE10_FILE_HASH_CACHE_DIR": str(self.root / "cache")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def test_second_call_is_served_from_cache_and_changes_invalidate_it(self):
        target = self.root / "a.pdf"
        target.write_bytes(b"first")
        calls = []

        def compute(path):
            calls.append(path)
            return _digest(path)

        self.assertEqual(file_hash_cache.cached_sha256(target, compute), hashlib.sha256(b"first").hexdigest())
        self.assertEqual(file_hash_cache.cached_sha256(target, compute), hashlib.sha256(b"first").hexdigest())
        self.assertEqual(len(calls), 1)
        target.write_bytes(b"second, longer")
        self.assertEqual(file_hash_cache.cached_sha256(target, compute), hashlib.sha256(b"second, longer").hexdigest())
        self.assertEqual(len(calls), 2)
        os.utime(target, ns=(1, 1))        # same bytes, other mtime: a miss, still the right digest
        self.assertEqual(file_hash_cache.cached_sha256(target, compute), hashlib.sha256(b"second, longer").hexdigest())
        self.assertEqual(len(calls), 3)

    def test_facts_of_different_kinds_share_one_record_and_survive_each_other(self):
        target = self.root / "d.pdf"
        target.write_bytes(b"%PDF-1.4 body")
        info_calls = []

        def info(path):
            info_calls.append(path)
            return [12, None]

        self.assertEqual(file_hash_cache.cached_fact(target, "pdf_info/x/1", info), [12, None])
        self.assertEqual(file_hash_cache.cached_sha256(target, _digest), hashlib.sha256(b"%PDF-1.4 body").hexdigest())
        self.assertEqual(file_hash_cache.cached_fact(target, "pdf_info/x/1", info), [12, None])
        self.assertEqual(len(info_calls), 1)                                   # served from the record
        self.assertEqual(len(list((self.root / "cache").rglob("*.json"))), 1)  # one record, two facts
        file_hash_cache.cached_fact(target, "pdf_info/y/1", info)              # another library build: a miss
        self.assertEqual(len(info_calls), 2)
        target.write_bytes(b"%PDF-1.4 other body")
        file_hash_cache.cached_fact(target, "pdf_info/x/1", info)              # changed file: a miss
        self.assertEqual(len(info_calls), 3)

    def test_corrupt_record_reads_as_a_miss(self):
        target = self.root / "b.pdf"
        target.write_bytes(b"payload")
        file_hash_cache.cached_sha256(target, _digest)
        for record in (self.root / "cache").rglob("*.json"):
            record.write_text("{not json", encoding="utf-8")
        calls = []
        self.assertEqual(file_hash_cache.cached_sha256(target, lambda p: calls.append(p) or _digest(p)),
                         hashlib.sha256(b"payload").hexdigest())
        self.assertEqual(len(calls), 1)

    def test_no_directory_means_no_cache(self):
        with mock.patch.dict(os.environ, {"CASE10_FILE_HASH_CACHE_DIR": "", "CASE10_LIVE_TAGGER_CACHE_DIR": ""}):
            target = self.root / "c.pdf"
            target.write_bytes(b"x")
            calls = []
            for _ in range(2):
                file_hash_cache.cached_sha256(target, lambda p: calls.append(p) or _digest(p))
            self.assertEqual(len(calls), 2)
            self.assertFalse((self.root / "cache").exists())

    def test_follows_the_live_tagger_cache_dir_when_not_set_explicitly(self):
        with mock.patch.dict(os.environ, {"CASE10_FILE_HASH_CACHE_DIR": "", "CASE10_LIVE_TAGGER_CACHE_DIR": str(self.root / "lt")}):
            self.assertEqual(file_hash_cache.cache_dir(), self.root / "lt" / "file_sha256")


class JsonGzipTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        big = {"rows": ["строка %d" % i for i in range(20000)]}

        @app.get("/big")
        def big_json():
            return big

        @app.get("/small")
        def small_json():
            return {"ok": True}

        @app.get("/blob")
        def blob():
            return Response(b"\x89PNG" + b"0" * 300000, media_type="image/png")

        @app.get("/error")
        def error():
            return JSONResponse({"detail": "x" * 200000}, status_code=404)

        app.add_middleware(LargeJsonGzipMiddleware, min_bytes=100 * 1024)
        self.client = TestClient(app)
        self.big = big

    def test_big_json_is_compressed_for_gzip_clients_and_decodes_to_the_same_body(self):
        plain = self.client.get("/big", headers={"Accept-Encoding": "identity"})
        packed = self.client.get("/big", headers={"Accept-Encoding": "gzip"})
        self.assertNotIn("content-encoding", plain.headers)
        self.assertEqual(packed.headers["content-encoding"], "gzip")
        self.assertIn("Accept-Encoding", packed.headers["vary"])
        self.assertLess(int(packed.headers["content-length"]), len(plain.content) / 3)   # httpx hands over the decoded body
        self.assertEqual(packed.content, plain.content)
        self.assertEqual(packed.json(), plain.json())

    def test_small_json_binary_and_error_responses_pass_through(self):
        for path in ("/small", "/blob", "/error"):
            response = self.client.get(path, headers={"Accept-Encoding": "gzip"})
            self.assertNotIn("content-encoding", response.headers, path)

    def test_no_gzip_without_accept_encoding(self):
        response = self.client.get("/big", headers={"Accept-Encoding": "identity"})
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(response.json(), self.big)


class CompactJsonResponseTests(unittest.TestCase):
    def test_body_equals_what_fastapi_would_send(self):
        from datetime import datetime, date
        from fastapi.encoders import jsonable_encoder

        from app.api.routes_case10 import _compact_json_response

        data = {"id": 7, "created_at": datetime(2026, 10, 19, 8, 30, 1, 123456), "day": date(2026, 10, 19), "none": None,
                "payload": {"sections": [{"название": "Раздел «1»", "значения": [1, 2.5, True, None, "x y"]}], 3: "int key"},
                "tuple": (1, 2), "set": {5}}
        self.assertEqual(_compact_json_response(data).body, JSONResponse(jsonable_encoder(data)).body)
        self.assertEqual(_compact_json_response(data).media_type, "application/json")


class QualityPrefetchTests(unittest.TestCase):
    """The pool only warms the on-disk cache; what the parent then reads is what it would have computed itself."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.docs = []
        import fitz
        from types import SimpleNamespace

        for index in range(2):
            pdf = fitz.open()
            for number in range(14):
                page = pdf.new_page(width=595, height=842)
                page.insert_text((72, 72), f"Лист {number + 1} помещение 1.10{index}", fontsize=12)
                if number == 3:
                    page.set_rotation(90)
            path = self.root / f"vol{index}.pdf"
            pdf.save(str(path))
            pdf.close()
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            self.docs.append(SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": path.name}},
                                             file_hash=sha, content_hash=sha))
        self.pairs = [(doc, page) for doc in self.docs for page in range(1, 15)]

    def tearDown(self):
        self._tmp.cleanup()

    def _results(self, cache: Path):
        from app.domain import official_evidence

        official_evidence._QUALITY_PAGE_CACHE.clear()
        with mock.patch.dict(os.environ, {"CASE10_ORIGINALS_ROOT": str(self.root), "CASE10_QUALITY_CACHE_DIR": str(cache)}):
            return [dict(official_evidence._quality_for_page(doc, page)) for doc, page in self.pairs]

    def test_prefetched_results_equal_serial_results_and_come_from_the_pool(self):
        from app.domain import official_evidence, quality_prefetch

        serial = self._results(self.root / "serial_cache")
        official_evidence._QUALITY_PAGE_CACHE.clear()
        cache = self.root / "pool_cache"
        with mock.patch.dict(os.environ, {"CASE10_ORIGINALS_ROOT": str(self.root), "CASE10_QUALITY_CACHE_DIR": str(cache)}),                 mock.patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_WORKERS", 2):
            report = quality_prefetch.prefetch(self.pairs)
            self.assertEqual(report["missing"], 28)
            self.assertEqual(report["workers"], 2)
            self.assertNotIn("pool_error", report)
            self.assertEqual(len(list(cache.rglob("*.json"))), 28)       # written by the workers
        pooled = self._results(cache)                                    # served from the files the pool wrote
        self.assertEqual(pooled, serial)
        self.assertTrue(any(row["reasons"] for row in serial))           # the rotated page is flagged: not all-empty

    def test_off_switch_and_missing_cache_dir_do_nothing(self):
        from app.domain import quality_prefetch

        with mock.patch.dict(os.environ, {"CASE10_ORIGINALS_ROOT": str(self.root), "CASE10_QUALITY_CACHE_DIR": str(self.root / "c"),
                                          "CASE10_QUALITY_PREFETCH": "0"}):
            self.assertEqual(quality_prefetch.prefetch(self.pairs)["workers"], 0)
        with mock.patch.dict(os.environ, {"CASE10_ORIGINALS_ROOT": str(self.root), "CASE10_QUALITY_CACHE_DIR": "",
                                          "CASE10_LIVE_TAGGER_CACHE_DIR": ""}):
            self.assertEqual(quality_prefetch.prefetch(self.pairs)["workers"], 0)


class EmbeddingDiskCacheTests(unittest.TestCase):
    def setUp(self):
        from app.domain import semantic_similarity

        self.mod = semantic_similarity
        self._tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"CASE10_EMBEDDING_CACHE_DIR": self._tmp.name})
        self.env.start()
        self.mod._embed_cached.cache_clear()

    def tearDown(self):
        self.env.stop()
        self.mod._embed_cached.cache_clear()
        self._tmp.cleanup()

    def test_second_process_reads_the_stored_vector_without_a_model(self):
        import numpy as np

        calls = []

        class FakeModel:
            def encode(self, text, normalize_embeddings=True):
                calls.append(text)
                return np.array([0.1, 0.2, 0.3 + len(text) * 1e-7], dtype=np.float32)

        with mock.patch.object(self.mod, "_get_model", return_value=FakeModel()):
            first = self.mod.embed("Площадь участка")
        self.assertEqual(calls, ["Площадь участка"])
        self.mod._embed_cached.cache_clear()                      # a new process: empty in-memory cache
        with mock.patch.object(self.mod, "_get_model", side_effect=AssertionError("model must not be needed")):
            second = self.mod.embed("Площадь   участка")           # same text after whitespace normalisation
        self.assertEqual(first.tobytes(), second.tobytes())
        self.assertEqual(first.dtype, second.dtype)

    def test_no_directory_means_no_files_and_a_missing_model_is_not_stored(self):
        with mock.patch.dict(os.environ, {"CASE10_EMBEDDING_CACHE_DIR": "", "CASE10_LIVE_TAGGER_CACHE_DIR": ""}):
            self.assertIsNone(self.mod._disk_cache_dir())
        with mock.patch.object(self.mod, "_get_model", return_value=None):
            self.assertIsNone(self.mod.embed("что-то"))
        self.assertEqual(list(Path(self._tmp.name).rglob("*.npy")), [])

    def test_corrupt_file_reads_as_a_miss(self):
        import numpy as np

        class FakeModel:
            def encode(self, text, normalize_embeddings=True):
                return np.ones(3, dtype=np.float32)

        with mock.patch.object(self.mod, "_get_model", return_value=FakeModel()):
            self.mod.embed("abc")
        for path in Path(self._tmp.name).rglob("*.npy"):
            path.write_bytes(b"garbage")
        self.mod._embed_cached.cache_clear()
        with mock.patch.object(self.mod, "_get_model", return_value=FakeModel()):
            self.assertEqual(self.mod.embed("abc").tolist(), [1.0, 1.0, 1.0])


class AuxPoolSizeTests(unittest.TestCase):
    def _count(self, pending, env, *names):
        from app.domain import aux_pool

        clean = {key: "" for key in ("CASE10_LIVE_TAGGER_WORKERS", "CASE10_EXPLICATION_WORKERS", "CASE10_FREE_HYPOTHESES_WORKERS")}
        with mock.patch.dict(os.environ, {**clean, **env}):
            return aux_pool.worker_count(pending, *names)

    def test_unset_keeps_the_old_default_of_three(self):
        self.assertEqual(self._count(10, {}, "CASE10_EXPLICATION_WORKERS"), 3)
        self.assertEqual(self._count(2, {}, "CASE10_EXPLICATION_WORKERS"), 2)

    def test_zero_means_the_taggers_automatic_size_not_one_worker(self):
        with mock.patch("app.domain.live_tagger_scan.available_cpu_count", return_value=6):
            self.assertEqual(self._count(10, {"CASE10_LIVE_TAGGER_WORKERS": "0"}, "CASE10_EXPLICATION_WORKERS"), 6)
            self.assertEqual(self._count(4, {"CASE10_LIVE_TAGGER_WORKERS": "0"}, "CASE10_EXPLICATION_WORKERS"), 4)

    def test_explicit_values_win_in_order_and_are_no_longer_cut_to_three(self):
        env = {"CASE10_LIVE_TAGGER_WORKERS": "8", "CASE10_EXPLICATION_WORKERS": "5"}
        self.assertEqual(self._count(10, env, "CASE10_FREE_HYPOTHESES_WORKERS", "CASE10_EXPLICATION_WORKERS"), 5)
        self.assertEqual(self._count(10, {"CASE10_LIVE_TAGGER_WORKERS": "8"}, "CASE10_EXPLICATION_WORKERS"), 8)
        self.assertEqual(self._count(10, {"CASE10_LIVE_TAGGER_WORKERS": "1"}, "CASE10_EXPLICATION_WORKERS"), 1)
        self.assertEqual(self._count(20, {"CASE10_LIVE_TAGGER_WORKERS": "99"}, "CASE10_EXPLICATION_WORKERS"), 12)
        self.assertEqual(self._count(10, {"CASE10_LIVE_TAGGER_WORKERS": "x"}, "CASE10_EXPLICATION_WORKERS"), 3)


class OcrCacheTests(unittest.TestCase):
    def setUp(self):
        import fitz

        from app.domain import dataset_sources, ocr_cache

        self.ds, self.cache = dataset_sources, ocr_cache
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        pdf = fitz.open()
        pdf.new_page(width=595, height=842)
        path = self.root / "scan.pdf"
        pdf.save(str(path))
        pdf.close()
        self.sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.env = mock.patch.dict(os.environ, {"CASE10_ORIGINALS_ROOT": str(self.root), "CASE10_OCR_CACHE_DIR": str(self.root / "ocr")})
        self.env.start()
        self.identity = mock.patch.object(ocr_cache, "_identity", return_value="test-identity")
        self.identity.start()
        self.ds._ocr_page_words.cache_clear()
        self.ds._ocr_original_clip.cache_clear()

    def tearDown(self):
        self.identity.stop()
        self.env.stop()
        self.ds._ocr_page_words.cache_clear()
        self.ds._ocr_original_clip.cache_clear()
        self._tmp.cleanup()

    def test_page_words_are_ocrd_once_then_served_from_disk_bit_for_bit(self):
        fake = [{"text": "Площадь", "bbox": (10.0, 20.0, 110.0, 40.0), "confidence": 91.5},
                {"text": "1.109", "bbox": (120.0, 20.0, 170.0, 40.0), "confidence": 88}]
        with mock.patch.object(self.ds, "_ocr_words_pixel_space", return_value=fake) as engine:
            first = self.ds._ocr_page_words("scan.pdf", self.sha, 1, "rus")
        self.assertEqual(engine.call_count, 1)
        self.ds._ocr_page_words.cache_clear()                        # another process
        with mock.patch.object(self.ds, "_ocr_words_pixel_space", side_effect=AssertionError("OCR must not run")):
            second = self.ds._ocr_page_words("scan.pdf", self.sha, 1, "rus")
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first), json.dumps(second))
        self.assertEqual(len(first), 2)

    def test_failures_are_not_stored_and_another_language_is_another_entry(self):
        with mock.patch.object(self.ds, "_ocr_words_pixel_space", return_value=None):
            self.assertIsNone(self.ds._ocr_page_words("scan.pdf", self.sha, 1, "rus"))
        self.assertEqual(list((self.root / "ocr").rglob("*.json")), [])
        self.cache.store_words(self.sha, 1, "rus", ({"text": "a", "bbox": [0, 0, 1, 1], "confidence": 1.0},))
        self.assertIsNotNone(self.cache.load_words(self.sha, 1, "rus"))
        self.assertIsNone(self.cache.load_words(self.sha, 1, "eng"))
        self.assertIsNone(self.cache.load_words(self.sha, 2, "rus"))

    def test_clip_text_roundtrip_and_empty_text_is_not_stored(self):
        self.cache.store_clip(self.sha, 1, (1.0, 2.0, 3.0, 4.0), "rus", "")
        self.assertIsNone(self.cache.load_clip(self.sha, 1, (1.0, 2.0, 3.0, 4.0), "rus"))
        self.cache.store_clip(self.sha, 1, (1.0, 2.0, 3.0, 4.0), "rus", "Кабинет врача")
        self.assertEqual(self.cache.load_clip(self.sha, 1, (1.0, 2.0, 3.0, 4.0), "rus"), "Кабинет врача")

    def test_unidentifiable_ocr_build_means_no_cache(self):
        with mock.patch.object(self.cache, "_identity", return_value=None):
            self.cache.store_words(self.sha, 1, "rus", ({"text": "a", "bbox": [0, 0, 1, 1], "confidence": 1.0},))
            self.assertIsNone(self.cache.load_words(self.sha, 1, "rus"))
        self.assertEqual(list((self.root / "ocr").rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
