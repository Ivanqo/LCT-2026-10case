"""Step E1: static guards for two delivery defects found when the image was first run.

1. `evaluation/export_identity.py` (step F1) was imported by `exporter.py` but missing from the Dockerfile COPY, the build
   whitelist and the delivery bundle list: the delivered image could not start the CLI.
2. The Node gateway exposes an explicit allow-list; the dashboard, the weekly ML report and the DOCX/XML exports (step Z) were
   missing from it, so the UI tabs and buttons failed through the gateway that is the UI's default API base.
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _evaluation_modules_needed() -> set[str]:
    """Modules of the `evaluation` package that the delivered runtime imports (app/ and, transitively, those modules)."""
    needed: set[str] = set()
    pattern = re.compile(r"^\s*from evaluation\.(\w+) import|^\s*from evaluation import (\w+)", re.M)
    for path in (REPO / "api_service" / "app").rglob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            needed.add(match.group(1) or match.group(2))
    relative = re.compile(r"^\s*from \.(\w+) import", re.M)
    queue = list(needed)
    while queue:
        name = queue.pop()
        source = REPO / "evaluation" / f"{name}.py"
        if not source.is_file():
            continue
        for match in relative.finditer(source.read_text(encoding="utf-8")):
            if match.group(1) not in needed:
                needed.add(match.group(1))
                queue.append(match.group(1))
    return needed


class EvaluationModulesAreShippedTests(unittest.TestCase):
    def test_runtime_imports_of_evaluation_are_copied_whitelisted_and_bundled(self) -> None:
        needed = _evaluation_modules_needed()
        self.assertIn("exporter", needed)
        dockerfile, dockerignore, bundle = _read("api_service/Dockerfile"), _read(".dockerignore"), _read("deploy/make_delivery.py")
        delivery = dockerfile.split("AS delivery", 1)[1]
        for name in sorted(needed):
            with self.subTest(module=name):
                self.assertIn(f"evaluation/{name}.py", delivery, "not copied into the delivery image")
                self.assertIn(f"!evaluation/{name}.py", dockerignore, "not on the build-context whitelist")
                self.assertIn(f'"evaluation/{name}.py"', bundle, "not in the delivery bundle list")


class GatewayExposesUiRoutesTests(unittest.TestCase):
    @staticmethod
    def _gateway_patterns() -> list[tuple[str, re.Pattern[str]]]:
        source = _read("node_gateway/server.js")
        return [(method, re.compile(pattern.replace("\\/", "/"))) for method, pattern in re.findall(r'\["(GET|POST|DELETE)", /(.+?)/\],', source)]

    def test_routes_called_by_the_frontend_are_exposed(self) -> None:
        patterns = self._gateway_patterns()
        api_js = _read("frontend_react/src/api.js")
        paths = {re.sub(r"\$\{[^}]+\}", "1", raw) for raw in re.findall(r"[`'](/api/[^`'?]+)", api_js)}
        paths |= {"/api/case10/protocols/1/docx", "/api/case10/protocols/1/xml", "/api/case10/protocols/1/pdf"}  # <a href> downloads
        self.assertIn("/api/case10/dashboard", paths)
        missing = []
        for path in sorted(paths):
            if not any(pattern.match(path) for _method, pattern in patterns):
                missing.append(path)
        self.assertEqual(missing, [], "frontend routes missing from the gateway allow-list")

    def test_gateway_openapi_lists_the_new_routes(self) -> None:
        text = _read("node_gateway/openapi.json")
        for path in ("/api/case10/dashboard", "/api/case10/ml/reports/weekly", "/api/case10/protocols/{protocol_id}/docx",
                     "/api/case10/protocols/{protocol_id}/xml"):
            self.assertIn(f'"{path}"', text)


if __name__ == "__main__":
    unittest.main()
