"""ТЗ 9.6 "Модуль интеграции по API с внешней ИС (целевая ИАИС РИН)"
(Low priority): outbound sync of a finalized protocol.

Covers `app/clients/iais_rin_client.py` and `app/domain/iais_rin_sync.py`
against a real local mock HTTP server (stdlib `http.server`, no fixture
service or extra dependency needed) standing in for ИАИС РИН, per the task's
"клиент против настраиваемого URL с mock-сервером для тестов" requirement.
"""
from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.api.auth import get_current_user
from app.api.routes_case10 import router as case10_router
from app.clients.iais_rin_client import IaisRinClient, IaisRinRejectedError, IaisRinRetryableError
from app.db.models import InspectionProcess, MonitoringMetric, Organization, Project, Protocol, User
from app.db.session import Base, get_db
from app.domain.iais_rin_sync import (
    IAIS_SYNC_NOT_CONFIGURED,
    IAIS_SYNC_PENDING_SYNC,
    IAIS_SYNC_REJECTED,
    IAIS_SYNC_SYNCED,
    retry_pending_iais_rin_syncs,
    sync_protocol_to_iais_rin,
    sync_state_of,
)


class _MockIaisRinServer:
    """A real local HTTP server standing in for ИАИС РИН. `queue_responses`
    schedules the status code(s) the next request(s) should get; once
    exhausted, every further request gets 200."""

    def __init__(self):
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._responses: list[int] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                with outer._lock:
                    outer.requests.append({"path": self.path, "body": json.loads(body or b"{}")})
                    status = outer._responses.pop(0) if outer._responses else 200
                payload = json.dumps({"status": "accepted"} if status < 400 else {"error": "mock failure"}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):  # noqa: A002 -- stdlib signature
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def queue_responses(self, *statuses: int) -> None:
        with self._lock:
            self._responses.extend(statuses)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class IaisRinClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = _MockIaisRinServer()

    def tearDown(self):
        self.server.shutdown()

    async def test_successful_send_returns_the_parsed_response(self):
        self.server.queue_responses(200)
        client = IaisRinClient(base_url=self.server.base_url, timeout=5.0)
        result = await client.send_inspection_result(process_id="proc-1", payload={"hello": "world"})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.server.requests[0]["path"], "/api/v1/inspection/proc-1")
        self.assertEqual(self.server.requests[0]["body"], {"hello": "world"})

    async def test_5xx_raises_retryable_error(self):
        self.server.queue_responses(503)
        client = IaisRinClient(base_url=self.server.base_url, timeout=5.0)
        with self.assertRaises(IaisRinRetryableError):
            await client.send_inspection_result(process_id="proc-1", payload={})

    async def test_4xx_raises_rejected_error_not_retryable(self):
        self.server.queue_responses(422)
        client = IaisRinClient(base_url=self.server.base_url, timeout=5.0)
        with self.assertRaises(IaisRinRejectedError) as ctx:
            await client.send_inspection_result(process_id="proc-1", payload={})
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_connection_failure_is_retryable(self):
        # Nothing listening on this port -- a real connection-level failure,
        # not a mocked one, exercising the same httpx.RequestError branch a
        # timeout would.
        client = IaisRinClient(base_url="http://127.0.0.1:1", timeout=2.0)
        with self.assertRaises(IaisRinRetryableError):
            await client.send_inspection_result(process_id="proc-1", payload={})

    async def test_unconfigured_client_raises_before_any_request(self):
        client = IaisRinClient(base_url="", timeout=5.0)
        self.assertFalse(client.configured)
        with self.assertRaises(Exception):
            await client.send_inspection_result(process_id="proc-1", payload={})
        self.assertEqual(self.server.requests, [])


class SyncProtocolToIaisRinTests(unittest.IsolatedAsyncioTestCase):
    """Exercises `sync_protocol_to_iais_rin` / `retry_pending_iais_rin_syncs`
    directly against a real (sqlite, in-memory) DB, with the client's target
    pointed at the mock server."""

    def setUp(self):
        self.server = _MockIaisRinServer()
        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

        org = Organization(name="IAIS Test Org", slug="iais-test-org")
        self.db.add(org)
        self.db.flush()
        project = Project(name="IAIS Project", organization_id=org.id)
        self.db.add(project)
        self.db.flush()
        process = InspectionProcess(
            id="proc-iais-1", project_id=project.id, organization_id=org.id, object_id="OBJ-1",
            status="FINALIZED",
        )
        self.db.add(process)
        self.db.flush()
        self.protocol = Protocol(
            process_id=process.id, project_id=project.id, organization_id=org.id, object_id="OBJ-1",
            version=1, matrix_version="v1", dataset_version="d1", model_version="m1",
            status="FINALIZED", payload_json={"findings": []},
        )
        self.db.add(self.protocol)
        self.db.commit()
        self.db.refresh(self.protocol)

    def tearDown(self):
        self.server.shutdown()
        self.db.close()

    def _client(self, **kwargs) -> IaisRinClient:
        return IaisRinClient(base_url=self.server.base_url, timeout=5.0, **kwargs)

    async def test_blocked_unless_protocol_finalized(self):
        self.protocol.status = "DRAFT"
        self.db.add(self.protocol)
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            await sync_protocol_to_iais_rin(self.db, self.protocol, client=self._client())
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.server.requests, [])

    async def test_not_configured_is_recorded_without_a_network_call(self):
        state = await sync_protocol_to_iais_rin(self.db, self.protocol, client=IaisRinClient(base_url="", timeout=5.0))
        self.assertEqual(state["status"], IAIS_SYNC_NOT_CONFIGURED)
        self.assertEqual(self.server.requests, [])

    async def test_successful_send_marks_protocol_synced_and_records_a_metric(self):
        self.server.queue_responses(200)
        state = await sync_protocol_to_iais_rin(self.db, self.protocol, client=self._client())
        self.assertEqual(state["status"], IAIS_SYNC_SYNCED)
        self.assertEqual(state["attempts"], 1)
        self.assertIsNotNone(state["synced_at"])
        self.assertIsNone(state["next_retry_at"])
        self.db.expire_all()
        self.assertEqual(sync_state_of(self.db.get(Protocol, self.protocol.id))["status"], IAIS_SYNC_SYNCED)
        self.assertEqual(self.server.requests[0]["path"], f"/api/v1/inspection/{self.protocol.process_id}")

        metrics = self.db.query(MonitoringMetric).filter_by(metric_name="iais_rin_sync_duration_seconds").all()
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].tags["outcome"], "success")

    async def test_5xx_response_schedules_a_one_minute_retry_first(self):
        self.server.queue_responses(500)
        now = datetime(2026, 1, 1, 0, 0, 0)
        state = await sync_protocol_to_iais_rin(self.db, self.protocol, client=self._client(), now=now)
        self.assertEqual(state["status"], IAIS_SYNC_PENDING_SYNC)
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(datetime.fromisoformat(state["next_retry_at"]), now + timedelta(minutes=1))

    async def test_rejected_4xx_response_does_not_schedule_a_retry(self):
        self.server.queue_responses(400)
        state = await sync_protocol_to_iais_rin(self.db, self.protocol, client=self._client())
        self.assertEqual(state["status"], IAIS_SYNC_REJECTED)
        self.assertIsNone(state["next_retry_at"])

    async def test_retry_delays_follow_the_1_5_15_minute_schedule_then_hourly(self):
        now = datetime(2026, 1, 1, 0, 0, 0)
        expected_deltas = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1)]
        for attempt, expected_delta in enumerate(expected_deltas, start=1):
            self.server.queue_responses(503)
            state = await sync_protocol_to_iais_rin(self.db, self.protocol, client=self._client(), now=now)
            self.assertEqual(state["attempts"], attempt)
            self.assertEqual(datetime.fromisoformat(state["next_retry_at"]), now + expected_delta)

    async def test_sweep_retries_only_due_pending_sync_protocols_and_can_succeed(self):
        # First attempt fails -> PENDING_SYNC with next_retry_at 1 minute out.
        self.server.queue_responses(500)
        now = datetime(2026, 1, 1, 0, 0, 0)
        await sync_protocol_to_iais_rin(self.db, self.protocol, client=self._client(), now=now)
        self.assertEqual(sync_state_of(self.protocol)["status"], IAIS_SYNC_PENDING_SYNC)

        # Sweeping before next_retry_at is due does nothing.
        too_early = await retry_pending_iais_rin_syncs(self.db, now=now + timedelta(seconds=30), client=self._client())
        self.assertEqual(too_early, [])
        self.assertEqual(len(self.server.requests), 1)  # no new request was made

        # Sweeping once it's due retries it; this time the mock server accepts.
        self.server.queue_responses(200)
        results = await retry_pending_iais_rin_syncs(self.db, now=now + timedelta(minutes=1, seconds=1), client=self._client())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], IAIS_SYNC_SYNCED)
        self.assertEqual(len(self.server.requests), 2)
        self.db.expire_all()
        self.assertEqual(sync_state_of(self.db.get(Protocol, self.protocol.id))["status"], IAIS_SYNC_SYNCED)

    async def test_sweep_ignores_protocols_that_were_never_synced_or_already_synced(self):
        # Never attempted at all -- no iais_rin_sync block yet.
        results = await retry_pending_iais_rin_syncs(self.db, client=self._client())
        self.assertEqual(results, [])

        self.server.queue_responses(200)
        await sync_protocol_to_iais_rin(self.db, self.protocol, client=self._client())
        # Already SYNCED -- a sweep must not resend it.
        results_after_success = await retry_pending_iais_rin_syncs(self.db, client=self._client())
        self.assertEqual(results_after_success, [])
        self.assertEqual(len(self.server.requests), 1)


class FinalizeTriggersIaisRinSyncTests(unittest.TestCase):
    """End-to-end: POST .../finalize actually calls the configured ИАИС РИН
    target, and its outcome lands on the protocol without breaking the
    finalize response itself."""

    def setUp(self):
        self.server = _MockIaisRinServer()
        self._old_base_url = settings.IAIS_RIN_BASE_URL
        settings.IAIS_RIN_BASE_URL = self.server.base_url

        self._tmp_uploads = tempfile.TemporaryDirectory()
        self._old_temp_uploads_dir = settings.TEMP_UPLOADS_DIR
        settings.TEMP_UPLOADS_DIR = Path(self._tmp_uploads.name)

        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

        org = Organization(name="IAIS Finalize Org", slug="iais-finalize-org")
        self.db.add(org)
        self.db.flush()
        self.user = User(
            login="iais-inspector", email="iais@test.local", password_hash="x",
            api_key="k", is_admin=False, role="INSPECTOR", organization_id=org.id,
        )
        project = Project(name="IAIS Finalize Project", organization_id=org.id)
        self.db.add_all([self.user, project])
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(project)
        self.project = project

        app = FastAPI()
        app.include_router(case10_router, prefix="/api")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        self.server.shutdown()
        settings.IAIS_RIN_BASE_URL = self._old_base_url
        settings.TEMP_UPLOADS_DIR = self._old_temp_uploads_dir
        self._tmp_uploads.cleanup()
        self.db.close()

    def _finalize_a_fresh_protocol(self):
        seeded = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        self.assertEqual(seeded.status_code, 200, seeded.text)
        process_id = seeded.json()["process_id"]
        groups = self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={process_id}").json()
        for row in [g for g in groups if g["finding_status"] == "CANDIDATE"]:
            self.client.post(f"/api/case10/evidence-groups/{row['id']}/decisions", json={"decision": "Confirm", "comment": "ok"})
        protocol = self.client.get(f"/api/case10/protocols/current?project_id={self.project.id}&process_id={process_id}").json()
        return protocol

    def test_finalize_syncs_to_the_configured_mock_target(self):
        self.server.queue_responses(200)
        protocol = self._finalize_a_fresh_protocol()
        finalize = self.client.post(f"/api/case10/protocols/{protocol['id']}/finalize")
        self.assertEqual(finalize.status_code, 200, finalize.text)
        self.assertEqual(finalize.json()["status"], "FINALIZED")

        status = self.client.get(f"/api/case10/protocols/{protocol['id']}/iais-rin-sync")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], IAIS_SYNC_SYNCED)
        self.assertEqual(len(self.server.requests), 1)

    def test_manual_retry_endpoint_and_sweep_endpoint_require_finalized_and_admin_respectively(self):
        self.server.queue_responses(503)
        protocol = self._finalize_a_fresh_protocol()
        finalize = self.client.post(f"/api/case10/protocols/{protocol['id']}/finalize")
        self.assertEqual(finalize.status_code, 200, finalize.text)
        self.assertEqual(
            self.client.get(f"/api/case10/protocols/{protocol['id']}/iais-rin-sync").json()["status"],
            IAIS_SYNC_PENDING_SYNC,
        )

        # A plain inspector cannot trigger the org-wide sweep endpoint.
        forbidden = self.client.post("/api/case10/iais-rin-sync/sweep")
        self.assertEqual(forbidden.status_code, 403, forbidden.text)

        self.server.queue_responses(200)
        retried = self.client.post(f"/api/case10/protocols/{protocol['id']}/iais-rin-sync/retry")
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["status"], IAIS_SYNC_SYNCED)


if __name__ == "__main__":
    unittest.main()
