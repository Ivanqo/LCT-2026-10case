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


if __name__ == "__main__":
    unittest.main()
