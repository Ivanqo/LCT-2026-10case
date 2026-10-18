"""Final / step Z: the weekly report for ML engineers (TZ module 10) -- endpoint, rules, small samples, CLI."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from app.db.models import EvidenceDecision, EvidenceGroup, InspectorEdit, MLRetrainingLog, User
from app.domain import ml_weekly_report as ml
from test_case10_final_z_modules import SeededBase


class WeeklyReportTests(SeededBase):
    def setUp(self):
        super().setUp()
        self.ml_engineer = User(login="ml", email="ml@z.test", password_hash="x", api_key="ml-key", is_admin=False, role="ML_ENGINEER",
                                organization_id=self.org_id)
        self.db.add(self.ml_engineer)
        self.db.commit()
        self.current_user = self.ml_engineer

    def add_decisions(self, group_id, decision, count, *, days_ago=1, reason=None):
        for _ in range(count):
            self.db.add(EvidenceDecision(evidence_group_id=group_id, decision=decision, reason_code=reason, comment="c",
                                         user_id=self.inspector.id, created_at=datetime.utcnow() - timedelta(days=days_ago)))
        self.db.commit()

    def get(self, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items())
        response = self.client.get(f"/api/case10/ml/reports/weekly?{query}")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def candidate(self):
        return self.db.query(EvidenceGroup).filter(EvidenceGroup.process_id == self.process_id, EvidenceGroup.finding_status == "CANDIDATE").first()

    def test_empty_history_is_reported_as_too_little_data_and_does_not_fail(self):
        report = self.get().json()
        self.assertFalse(report["data_sufficiency"]["enough"])
        self.assertIn("Данных мало", report["data_sufficiency"]["note"])
        self.assertEqual(report["totals"]["decisions"], 0)
        self.assertIsNone(report["totals"]["precision_by_inspectors"])
        self.assertEqual((report["by_section"], report["by_method"], report["top_reject_reasons"], report["recommendations"]), ([], [], [], []))
        self.assertIsNone(report["retraining"]["latest"])
        self.assertIn("Данных мало", self.get(format="html").text)

    def test_roles_and_input_validation(self):
        self.current_user = self.inspector
        self.assertEqual(self.client.get("/api/case10/ml/reports/weekly").status_code, 403)
        self.current_user = self.ml_engineer
        self.assertEqual(self.client.get("/api/case10/ml/reports/weekly?end=вчера").status_code, 422)
        self.assertEqual(self.client.get("/api/case10/ml/reports/weekly?format=pdf").status_code, 422)

    def test_counts_reasons_change_and_recommendations(self):
        self.seed()
        group = self.candidate()
        method = (group.delta or {}).get("source") or "unknown"
        self.add_decisions(group.id, "Reject", 12, reason="WRONG_LOCALIZATION")
        self.add_decisions(group.id, "Reject", 2, reason="DATA_ERROR")
        self.add_decisions(group.id, "Confirm", 5)
        self.add_decisions(group.id, "Clarification Required", 2)
        self.add_decisions(group.id, "Confirm", 9, days_ago=9)           # previous week
        self.add_decisions(group.id, "Reject", 2, days_ago=9, reason="OTHER")
        self.add_decisions(group.id, "Confirm", 50, days_ago=40)         # outside both windows
        report = self.get().json()
        totals = report["totals"]
        self.assertEqual((totals["decisions"], totals["confirmed"], totals["rejected"], totals["clarified"]), (21, 5, 14, 2))
        self.assertAlmostEqual(totals["precision_by_inspectors"], 5 / 19, places=3)
        low, high = totals["precision_wilson95"]
        self.assertTrue(0 <= low < totals["precision_by_inspectors"] < high <= 1)
        self.assertEqual(report["previous_week_totals"]["decisions"], 11)
        self.assertEqual(report["change"]["decisions"], 10)
        self.assertAlmostEqual(report["change"]["precision_by_inspectors"], round(5 / 19 - 9 / 11, 4), places=3)
        self.assertTrue(report["data_sufficiency"]["enough"])
        reasons = {row["reason_code"]: row for row in report["top_reject_reasons"]}
        self.assertEqual((reasons["WRONG_LOCALIZATION"]["count"], reasons["DATA_ERROR"]["count"]), (12, 2))
        self.assertEqual(report["top_reject_reasons"][0]["reason_code"], "WRONG_LOCALIZATION")
        self.assertEqual(reasons["OTHER"]["previous_week"], 2)
        self.assertEqual(report["by_method"][0]["name"], method)
        self.assertEqual(report["by_method"][0]["decisions"], 21)
        rules = {item["rule"]: item for item in report["recommendations"]}
        self.assertIn("R1_METHOD_REJECTED", rules)       # 14 of 21 rejected = 67 %
        self.assertIn("R2_SECTION_REJECTED", rules)
        self.assertIn("R3_TOP_REASON", rules)             # 12 of 14 rejections are one reason
        self.assertIn("R4_PRECISION_DROP", rules)         # 82 % -> 26 %
        self.assertIn("привязк", rules["R3_TOP_REASON"]["text"])
        self.assertEqual(rules["R1_METHOD_REJECTED"]["evidence"], {"rejected": 14, "decisions": 21})
        html = self.get(format="html")
        self.assertTrue(html.headers["content-type"].startswith("text/html"))
        self.assertIn("WRONG_LOCALIZATION", html.text)

    def test_small_samples_do_not_trigger_rules_and_other_week_is_selectable(self):
        self.seed()
        self.add_decisions(self.candidate().id, "Reject", 5, reason="NOT_A_VIOLATION")
        report = self.get().json()
        self.assertFalse(report["data_sufficiency"]["enough"])
        self.assertEqual(report["recommendations"], [])
        self.assertEqual(report["totals"]["rejected"], 5)
        old = (datetime.utcnow() - timedelta(days=30)).date().isoformat()
        self.assertEqual(self.get(end=old).json()["totals"]["decisions"], 0)
        today = datetime.utcnow().date().isoformat()
        self.assertEqual(self.get(end=today).json()["totals"]["decisions"], 5)

    def test_hypothesis_reviews_and_retraining_log_are_included(self):
        self.seed()
        group = self.db.query(EvidenceGroup).filter(EvidenceGroup.process_id == self.process_id).first()
        for index, action in enumerate(("CONFIRMED", "REJECTED", "REJECTED"), start=1):
            self.db.add(InspectorEdit(organization_id=self.org_id, project_id=self.project.id, process_id=self.process_id, evidence_group_id=group.id,
                                      entity_type="HYPOTHESIS_REVIEW", entity_key=f"hypothesis:{group.id}:{index}", action=action, version=1,
                                      reason="r", user_id=self.inspector.id, created_at=datetime.utcnow() - timedelta(hours=2)))
        self.db.add(MLRetrainingLog(project_id=self.project.id, organization_id=self.org_id, dataset_version="ds-1", candidate_model_version="m-2",
                                    gate_status="BLOCKED", approval_status="BLOCKED", gate_reasons=["too few confirmed violations"]))
        self.db.commit()
        report = self.get().json()
        self.assertEqual((report["hypotheses"]["confirmed"], report["hypotheses"]["rejected"]), (1, 2))
        self.assertEqual(report["by_section"][0]["name"], ml.HYPOTHESIS_SECTION)
        self.assertEqual(report["retraining"]["latest"]["candidate_model_version"], "m-2")

    def test_wilson_interval_matches_known_value(self):
        low, high = ml.wilson(8, 10)
        self.assertAlmostEqual(low, 0.49, places=2)
        self.assertAlmostEqual(high, 0.943, places=2)
        self.assertIsNone(ml.wilson(0, 0))


class WeeklyReportCliTests(unittest.TestCase):
    def test_cli_runs_on_an_empty_database(self):
        from sqlalchemy import create_engine

        from app import cli
        from app.db.session import Base

        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite+pysqlite:///{Path(tmp, 'service.db').as_posix()}"
            engine = create_engine(url)
            Base.metadata.create_all(engine)
            engine.dispose()
            out = Path(tmp, "report.json")
            self.assertEqual(cli.main(["weekly-report", "--db-url", url, "--out", str(out)]), 0)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertFalse(report["data_sufficiency"]["enough"])
            html = Path(tmp, "report.html")
            self.assertEqual(cli.main(["weekly-report", "--db-url", url, "--format", "html", "--out", str(html), "--end", "2026-10-18"]), 0)
            self.assertIn("Данных мало", html.read_text(encoding="utf-8"))
            self.assertEqual(cli.main(["weekly-report", "--db-url", url, "--end", "x"]), 1)


if __name__ == "__main__":
    unittest.main()
