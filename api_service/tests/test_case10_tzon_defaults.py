"""TZ-ON: optional TZ functions are on by default (except the emulated pull); the weekly-report scheduler lives only in
the long-lived API process, starts once, takes a lock file and survives an empty database."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.session import Base
from app.domain import ml_weekly_report as ml
from app.domain.v3_pipeline import document_completeness

REPO = Path(__file__).resolve().parents[2]
FLAGS = {
    "CASE10_PARTIAL_UPLOAD_STATUS_ENABLED": "1",
    "CASE10_DOCX_TEXT_EXTRACTION_ENABLED": "1",
    "CASE10_ML_WEEKLY_REPORT_SCHEDULER_ENABLED": "1",
    "CASE10_IAIS_RIN_PULL_ENABLED": "0",
}


def _child(code: str, extra_env: dict[str, str] | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k not in FLAGS}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=180, check=True).stdout.strip()


class DefaultsTests(unittest.TestCase):
    def test_code_defaults_without_environment(self):
        out = _child("from app.config import settings as s; print(s.PARTIAL_UPLOAD_STATUS_ENABLED, s.DOCX_TEXT_EXTRACTION_ENABLED, "
                     "s.ML_WEEKLY_REPORT_SCHEDULER_ENABLED, s.IAIS_RIN_PULL_ENABLED, s.ML_WEEKLY_REPORT_INTERVAL_SECONDS)")
        self.assertEqual(out, "True True True False 604800")

    def test_environment_can_switch_each_option_off(self):
        out = _child("from app.config import settings as s; print(s.PARTIAL_UPLOAD_STATUS_ENABLED, s.DOCX_TEXT_EXTRACTION_ENABLED, "
                     "s.ML_WEEKLY_REPORT_SCHEDULER_ENABLED)",
                     {name: "0" for name in FLAGS})
        self.assertEqual(out, "False False False")

    def test_dockerfile_compose_env_example_and_readme_agree_with_code(self):
        dockerfile = (REPO / "api_service/Dockerfile").read_text(encoding="utf-8")
        compose = (REPO / "docker-compose.case10.yml").read_text(encoding="utf-8")
        env_example = (REPO / "deploy/case10/case10.env.example").read_text(encoding="utf-8")
        readme = (REPO / "README_GRADER.md").read_text(encoding="utf-8")
        deployment = (REPO / "DEPLOYMENT.md").read_text(encoding="utf-8")
        for name, value in FLAGS.items():
            self.assertIn(f"    {name}={value} ", dockerfile, name)
            self.assertIn(f"{name}: ${{{name}:-{value}}}", compose)
            self.assertIn(f"\n{name}={value}\n", env_example)
            self.assertIn(f"| `{name}` | `{value}` |", readme)
            self.assertIn(name, deployment if value == "1" else readme)


class SchedulerProcessScopeTests(unittest.TestCase):
    def test_cli_and_worker_never_import_the_api_app_or_the_scheduler(self):
        out = _child("import sys, app.cli, app.worker_case10; "
                     "print('app.main' in sys.modules, 'app.domain.ml_weekly_scheduler' in sys.modules)")
        self.assertEqual(out, "False False")

    def test_in_process_test_client_without_lifespan_does_not_start_it(self):
        from fastapi.testclient import TestClient
        from app import main as app_main

        TestClient(app_main.app).get("/health")
        self.assertIsNone(app_main._weekly_report_task)

    def test_api_startup_creates_one_task_and_shutdown_stops_it(self):
        from app import main as app_main

        calls: list[int] = []

        async def fake_scheduler(*args, **kwargs):
            calls.append(1)
            await asyncio.sleep(3600)

        async def scenario():
            with patch.object(settings, "ML_WEEKLY_REPORT_SCHEDULER_ENABLED", True), \
                    patch("app.domain.ml_weekly_scheduler.run_scheduler", fake_scheduler):
                await app_main._start_weekly_report_scheduler()
                first = app_main._weekly_report_task
                await app_main._start_weekly_report_scheduler()
                self.assertIs(app_main._weekly_report_task, first)
                await asyncio.sleep(0)
                await app_main._stop_weekly_report_scheduler()
                self.assertIsNone(app_main._weekly_report_task)

        asyncio.run(scenario())
        self.assertEqual(calls, [1])

    def test_disabled_flag_starts_nothing(self):
        from app import main as app_main

        async def scenario():
            with patch.object(settings, "ML_WEEKLY_REPORT_SCHEDULER_ENABLED", False):
                await app_main._start_weekly_report_scheduler()
                self.assertIsNone(app_main._weekly_report_task)

        asyncio.run(scenario())


class SchedulerTickTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(bind=engine)
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name) / "reports"

    def tearDown(self):
        self._tmp.cleanup()

    def tick(self, now):
        return ml.run_scheduler_tick(now=now, session_factory=self.factory, directory=self.directory, interval_seconds=7 * 86400)

    def test_empty_database_saves_one_report_and_marker_blocks_the_next_tick(self):
        start = datetime(2026, 10, 19, 12, 0)
        first = self.tick(start)
        self.assertTrue(first["ran"])
        self.assertEqual(first["reports_saved"], 1)
        self.assertTrue((self.directory / ".weekly_scheduler_state.json").is_file())
        self.assertFalse((self.directory / ".weekly_scheduler.lock").exists())
        self.assertFalse(self.tick(start + timedelta(hours=1))["ran"])
        self.assertEqual(len(list(self.directory.glob("weekly_*.json"))), 1)

    def test_fresh_lock_of_another_process_skips_the_tick(self):
        self.directory.mkdir(parents=True)
        lock = self.directory / ".weekly_scheduler.lock"
        lock.write_text("", encoding="utf-8")
        result = self.tick(datetime(2026, 10, 19, 12, 0))
        self.assertEqual((result["ran"], result["reports_saved"]), (False, 0))
        self.assertTrue(lock.exists())
        self.assertEqual(list(self.directory.glob("weekly_*.json")), [])

    def test_stale_lock_is_taken_over(self):
        self.directory.mkdir(parents=True)
        lock = self.directory / ".weekly_scheduler.lock"
        lock.write_text("", encoding="utf-8")
        old = time.time() - 3600
        os.utime(lock, (old, old))
        self.assertTrue(self.tick(datetime(2026, 10, 19, 12, 0))["ran"])
        self.assertFalse(lock.exists())


class PartialStatusNeverWorseThanBeforeTests(unittest.TestCase):
    def test_stage_with_files_but_no_registry_match_is_partial_not_missing(self):
        from types import SimpleNamespace

        summary = [{"doc_stage": "PD", "file_id": "PD-1", "file_name": "a.pdf"}, {"doc_stage": "PD", "file_id": "PD-2", "file_name": "b.pdf"}]
        handmade = SimpleNamespace(doc_stage="project", document_stage="project", dataset_stage="PD", dataset_split="MANUAL_UPLOAD",
                                   dataset_file_id=None, dataset_metadata={"document_manifest": {"registry_manifest_summary": summary}})
        self.assertEqual(document_completeness([handmade], registry_aware=True)["PD_UPLOADED"], "PD_PARTIAL")
        self.assertEqual(document_completeness([handmade], registry_aware=False)["PD_UPLOADED"], "PD_UPLOADED")


if __name__ == "__main__":
    unittest.main()
