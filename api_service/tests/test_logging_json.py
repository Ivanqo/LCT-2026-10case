from __future__ import annotations

import json
import logging
import sys
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.logging_setup import JsonLogFormatter
from app.request_context import RequestIDMiddleware, get_request_id, set_request_id, set_user_id

REQUIRED_FIELDS = {"timestamp", "level", "service", "message", "request_id", "user_id"}


def _make_record(**overrides) -> logging.LogRecord:
    kwargs = dict(
        name="api",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    kwargs.update(overrides)
    return logging.LogRecord(**kwargs)


class JsonLogFormatterTests(unittest.TestCase):
    """ТЗ 13 smoke test: a formatted record round-trips through json.loads and
    always carries the required keys, even when request_id/user_id are unset."""

    def setUp(self):
        set_request_id(None)
        set_user_id(None)

    def test_smoke_json_parses_with_required_fields(self):
        formatter = JsonLogFormatter("api")
        line = formatter.format(_make_record())

        payload = json.loads(line)  # must not raise

        self.assertTrue(REQUIRED_FIELDS.issubset(payload.keys()))
        self.assertEqual(payload["message"], "hello world")
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["service"], "api")
        self.assertIsNone(payload["request_id"])
        self.assertIsNone(payload["user_id"])
        self.assertTrue(payload["timestamp"].endswith("Z"))

    def test_request_id_and_user_id_come_from_context(self):
        set_request_id("req-123")
        set_user_id(42)
        formatter = JsonLogFormatter("api")

        payload = json.loads(formatter.format(_make_record()))

        self.assertEqual(payload["request_id"], "req-123")
        self.assertEqual(payload["user_id"], 42)

    def test_exception_is_captured_without_breaking_json(self):
        formatter = JsonLogFormatter("api")
        try:
            raise ValueError("boom")
        except ValueError:
            record = _make_record(level=logging.ERROR, exc_info=sys.exc_info())

        payload = json.loads(formatter.format(record))

        self.assertIn("exception", payload)
        self.assertIn("ValueError: boom", payload["exception"])


class RequestIDMiddlewareTests(unittest.TestCase):
    """Covers ТЗ 13's request_id contract: generate a uuid4 when the caller
    sends none, otherwise propagate the caller's own X-Request-ID."""

    def setUp(self):
        set_request_id(None)
        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)

        @app.get("/probe")
        def probe():
            return {"request_id": get_request_id()}

        self.client = TestClient(app)

    def test_generates_request_id_when_absent(self):
        resp = self.client.get("/probe")

        header_id = resp.headers["x-request-id"]
        self.assertTrue(header_id)
        self.assertEqual(resp.json()["request_id"], header_id)

    def test_propagates_incoming_request_id(self):
        resp = self.client.get("/probe", headers={"X-Request-ID": "client-supplied-id"})

        self.assertEqual(resp.headers["x-request-id"], "client-supplied-id")
        self.assertEqual(resp.json()["request_id"], "client-supplied-id")


if __name__ == "__main__":
    unittest.main()
