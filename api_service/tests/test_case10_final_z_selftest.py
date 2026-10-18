"""Final / step Z: `python -m app.cli selftest` -- the scenario registry, the runner and the command wiring.

The full self-check takes about a minute and a half (every PDF scenario runs the live tagger); the tests run the fast
structured scenarios and one PDF scenario, and check the runner's failure handling with a stub registry."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from app import selftest

REPO = Path(__file__).resolve().parents[2]


def run(**kwargs):
    lines: list[str] = []
    with patch.dict(os.environ, {}):
        code = selftest.run_selftest(say=lines.append, **kwargs)
    return code, lines


class RegistryTests(unittest.TestCase):
    def test_between_20_and_30_scenarios_with_positive_and_negative_kinds(self):
        self.assertTrue(20 <= len(selftest.SCENARIOS) <= 30, len(selftest.SCENARIOS))
        kinds = {kind for _name, kind, _function in selftest.SCENARIOS}
        self.assertTrue({"positive", "negative", "abstain", "stability"} <= kinds, kinds)
        names = [name for name, _kind, _function in selftest.SCENARIOS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_expert_example_has_a_scenario(self):
        text = " ".join(name.lower() for name, _kind, _function in selftest.SCENARIOS)
        for needle in ("room number repeated", "any order", "added in rd", "equal values", "wrong section"):
            self.assertIn(needle, text)


class RunnerTests(unittest.TestCase):
    def test_structured_scenarios_pass_and_the_summary_says_it_is_not_a_metric(self):
        code, lines = run(only="room")
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertTrue(lines[-1].startswith("selftest: "), lines[-1])
        self.assertIn("NOT a quality metric", lines[-1])
        self.assertEqual(sum(1 for line in lines if line.startswith("[PASS]")), sum(1 for name, _k, _f in selftest.SCENARIOS if "room" in name.lower()))

    def test_a_pdf_scenario_runs_through_the_pipeline(self):
        code, lines = run(only="changed value is reported")
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertIn("CANDIDATE 1000,5 -> 1200,0", lines[0])

    def test_failure_and_crash_are_reported_and_give_exit_code_1(self):
        def failing():
            raise AssertionError("values differ")

        def crashing():
            raise RuntimeError("boom")

        with patch.object(selftest, "SCENARIOS", [("a failing one", "positive", failing), ("a crashing one", "negative", crashing), ("ok", "abstain", lambda: "fine")]):
            code, lines = run()
        self.assertEqual(code, 1)
        self.assertEqual([line.split("]")[0] for line in lines[:3]], ["[FAIL", "[FAIL", "[PASS"])
        self.assertIn("RuntimeError: boom", lines[1])
        self.assertIn("1 PASS, 2 FAIL", lines[-1])

    def test_json_output(self):
        code, lines = run(only="room number repeated", as_json=True)
        data = json.loads(lines[0])
        self.assertEqual((code, data["summary"]["failed"], data["results"][0]["result"]), (0, 0, "PASS"))
        self.assertIn("not a quality metric", data["summary"]["note"])


class CommandTests(unittest.TestCase):
    def test_cli_command_exit_codes(self):
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "api_service"), str(REPO)]), "PYTHONIOENCODING": "utf-8"}
        done = subprocess.run([sys.executable, "-m", "app.cli", "selftest", "--only", "room number repeated"], cwd=REPO, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("1 PASS, 0 FAIL", done.stdout)


if __name__ == "__main__":
    unittest.main()
