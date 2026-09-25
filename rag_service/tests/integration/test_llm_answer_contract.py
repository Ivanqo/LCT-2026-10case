from __future__ import annotations

import html
import json
import os
import re
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from rag_service.app import main as rag_main
from rag_service.app.db.session import init_db
from rag_service.tests.integration.test_documentation_search import (
    PROJECT_ROOT,
    TEST_ROOT,
    FakeVectorStores,
    _as_int,
    _assert_pdf_runtime_ready,
    _contains_term,
    _fixture_paths,
    _matching_expected_sources,
    _source_digest,
    fake_get_vectorstores,
)


REPORT_DIR = Path(
    os.getenv(
        "RAG_LLM_CONTRACT_REPORT_DIR",
        str(PROJECT_ROOT / "rag_service" / "test_reports" / "llm_answer_contract"),
    )
).resolve()

LLM_CASES: list[dict[str, Any]] = [
    {
        "id": "m1_steel_grade",
        "question": "Какая марка стали указана для закладной детали М-1 для сборного марша?",
        "expected_filename_part": "КМ1",
        "expected_pages": [5],
        "answer_fact_groups": [["С245"], ["М-1", "закладн"]],
        "source_terms": ["Закладная деталь М-1", "С245"],
    },
    {
        "id": "stair_rebar_jointing",
        "question": "Как должно выполняться армирование лестничных маршей и площадок?",
        "expected_filename_part": "КЖ1.24",
        "expected_pages": [8],
        "answer_fact_groups": [["цельн"], ["без соедин", "нахлест"]],
        "source_terms": ["Армирование", "без соединений"],
    },
    {
        "id": "engineering_shafts_masonry_fact",
        "question": "Когда выполнять кладку инженерных шахт и чем заделывать отверстия после коммуникаций?",
        "expected_filename_part": "АР3",
        "expected_pages": [7],
        "answer_fact_groups": [["после прокладки"], ["инженерных коммуникац"], ["задел"]],
        "source_terms": ["Кладку инженерных шахт", "инженерных коммуникаций"],
    },
]

FORBIDDEN_ANSWER_TERMS = [
    "chunk_id",
    "CHUNK_ID",
    "embedding",
    "retriever",
    "score",
    "asset",
    "region",
    "JSON",
    "```",
    "LLM-сервис сейчас недоступен",
]


class LlmCallRecorder:
    def __init__(self, original: Any) -> None:
        self.original = original
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[str, list[int], list[int], str]:
        started = time.perf_counter()
        call: dict[str, Any] = {
            "question": kwargs.get("question"),
            "chunks_available": [c.get("chunk_id") for c in kwargs.get("chunks", [])],
            "error": None,
            "duration_ms": None,
            "raw_preview": "",
            "selected_chunk_ids": [],
            "answer_preview": "",
        }
        self.calls.append(call)
        try:
            answer, selected_chunk_ids, selected_asset_ids, raw = self.original(**kwargs)
        except Exception as exc:
            call["duration_ms"] = int((time.perf_counter() - started) * 1000)
            call["error"] = f"{exc.__class__.__name__}: {exc}"
            raise
        call["duration_ms"] = int((time.perf_counter() - started) * 1000)
        call["raw_preview"] = re.sub(r"\s+", " ", str(raw or "")).strip()[:1200]
        call["selected_chunk_ids"] = [int(x) for x in selected_chunk_ids]
        call["answer_preview"] = re.sub(r"\s+", " ", str(answer or "")).strip()[:1200]
        return answer, selected_chunk_ids, selected_asset_ids, raw


def _write_report(report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / "llm_answer_contract_report.json"
    html_path = REPORT_DIR / "index.html"
    report["report_files"] = {"json": str(json_path), "html": str(html_path)}
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(_render_report_html(report), encoding="utf-8")


def _summarize_report(report: dict[str, Any]) -> None:
    cases = report.get("cases", [])
    passed = sum(1 for case in cases if case.get("status") == "passed")
    failed = sum(1 for case in cases if case.get("status") != "passed")
    report["summary"] = {
        "total_cases": len(cases),
        "passed": passed,
        "failed": failed,
        "llm_calls": sum(1 for case in cases if case.get("llm_call", {}).get("duration_ms") is not None),
        "exact_selected_sources": sum(1 for case in cases if case.get("exact_selected_source")),
        "avg_answer_chars": round(
            sum(int(case.get("answer_chars") or 0) for case in cases) / len(cases),
            1,
        )
        if cases
        else 0,
    }


def _render_report_html(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    cases = report.get("cases", [])
    ingestions = report.get("ingestions", [])

    def pill(value: Any, css: str = "") -> str:
        return f'<span class="pill {css}">{html.escape(str(value))}</span>'

    def source_cards(items: list[dict[str, Any]]) -> str:
        if not items:
            return '<p class="empty">Нет источников.</p>'
        cards = []
        for src in items:
            cls = "source exact" if src.get("exact_match") else "source"
            cards.append(
                f"""
                <article class="{cls}">
                  <div class="source-title">
                    <strong>{html.escape(str(src.get("filename")))}</strong>
                    <span>лист {html.escape(str(src.get("page_number")))}</span>
                  </div>
                  <p>{html.escape(str(src.get("text_preview") or ""))}</p>
                </article>
                """
            )
        return "".join(cards)

    case_html = []
    for case in cases:
        status = str(case.get("status") or "unknown")
        css = "pass" if status == "passed" else "fail"
        errors = "".join(f"<li>{html.escape(str(error))}</li>" for error in case.get("errors", []))
        if not errors:
            errors = '<li class="ok">Ошибок нет.</li>'
        fact_checks = "".join(
            f"<li>{pill('OK' if item.get('matched') else 'FAIL', 'ok-pill' if item.get('matched') else 'bad-pill')} "
            f"{html.escape(str(item.get('group')))}</li>"
            for item in case.get("fact_checks", [])
        )
        forbidden = "".join(
            f"<li>{pill('OK' if not item.get('found') else 'FAIL', 'ok-pill' if not item.get('found') else 'bad-pill')} "
            f"{html.escape(str(item.get('term')))}</li>"
            for item in case.get("forbidden_checks", [])
        )
        llm_call = case.get("llm_call") or {}
        case_html.append(
            f"""
            <article class="case {css}">
              <div class="case-head">
                <div>
                  <h2>{html.escape(str(case.get("id")))}</h2>
                  <p>{html.escape(str(case.get("question")))}</p>
                </div>
                <span class="status">{html.escape(status)}</span>
              </div>
              <section class="answer">
                <h3>Ответ пользователю</h3>
                <p>{html.escape(str(case.get("answer") or ""))}</p>
              </section>
              <div class="grid">
                <section>
                  <h3>Факты</h3>
                  <ul>{fact_checks}</ul>
                </section>
                <section>
                  <h3>Служебные маркеры</h3>
                  <ul>{forbidden}</ul>
                </section>
                <section>
                  <h3>Источники</h3>
                  <p>Ожидались страницы: {' '.join(pill(p, 'info') for p in case.get("expected_pages", []))}</p>
                  <p>Точный выбранный источник: {pill(case.get("exact_selected_source"), 'ok-pill' if case.get("exact_selected_source") else 'bad-pill')}</p>
                </section>
                <section>
                  <h3>LLM вызов</h3>
                  <p>Время: {html.escape(str(llm_call.get("duration_ms")))} ms</p>
                  <p>Ошибка: {html.escape(str(llm_call.get("error") or "нет"))}</p>
                </section>
              </div>
              <ul class="errors">{errors}</ul>
              <h3>Выбранные источники</h3>
              <div class="sources">{source_cards(case.get("selected_sources", []))}</div>
              <h3>Все источники из ответа</h3>
              <div class="sources">{source_cards(case.get("sources", []))}</div>
            </article>
            """
        )

    ingestion_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('filename')))}</td>"
        f"<td>{html.escape(str(item.get('document_id')))}</td>"
        f"<td>{html.escape(str(item.get('chunks_created')))}</td>"
        f"<td>{html.escape(str(item.get('duration_ms')))} ms</td>"
        "</tr>"
        for item in ingestions
    )

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LLM Answer Contract Report</title>
  <style>
    :root {{
      --ink: #182230;
      --muted: #667085;
      --line: #d0d7e2;
      --panel: #ffffff;
      --surface: #f4f7fb;
      --ok: #14714b;
      --ok-bg: #eaf7ef;
      --bad: #b42318;
      --bad-bg: #fff0ed;
      --info-bg: #e8f3ff;
    }}
    body {{ margin: 0; font-family: Inter, Segoe UI, Arial, sans-serif; color: var(--ink); background: var(--surface); }}
    header {{ padding: 30px 32px 22px; background: #fff; border-bottom: 1px solid var(--line); }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 28px; }}
    h3 {{ font-size: 14px; margin: 18px 0 10px; color: #344054; }}
    main {{ padding: 24px 32px 40px; max-width: 1240px; margin: 0 auto; }}
    .stats {{ display: grid; grid-template-columns: repeat(5, minmax(140px, 1fr)); gap: 12px; margin-bottom: 24px; }}
    .stat, .case, table {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 1px 2px rgba(16, 24, 40, .04); }}
    .stat {{ padding: 16px; border-left: 4px solid #2563a8; }}
    .stat.good {{ border-left-color: var(--ok); }}
    .stat.bad {{ border-left-color: var(--bad); }}
    .stat strong {{ display: block; font-size: 28px; }}
    .stat span, .meta {{ color: var(--muted); font-size: 13px; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
    th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--line); }}
    th {{ color: var(--muted); font-size: 13px; }}
    .cases {{ display: grid; gap: 16px; }}
    .case {{ padding: 20px; border-left: 5px solid #7a8fa8; }}
    .case.pass {{ border-left-color: var(--ok); }}
    .case.fail {{ border-left-color: var(--bad); }}
    .case-head {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }}
    .case-head p {{ margin: 6px 0 0; color: #344054; }}
    .status, .pill {{ display: inline-flex; align-items: center; min-height: 24px; padding: 0 8px; border-radius: 999px; background: #edf2f7; font-size: 12px; font-weight: 700; }}
    .case.pass .status, .ok-pill {{ color: var(--ok); background: var(--ok-bg); }}
    .case.fail .status, .bad-pill {{ color: var(--bad); background: var(--bad-bg); }}
    .info {{ background: var(--info-bg); }}
    .answer {{ margin-top: 16px; padding: 14px; border: 1px solid var(--line); border-radius: 8px; background: #fbfcfe; }}
    .answer p {{ white-space: pre-wrap; line-height: 1.5; margin: 8px 0 0; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 12px; margin-top: 16px; }}
    .grid section {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fff; }}
    ul {{ margin: 0; padding-left: 20px; }}
    .errors {{ margin: 16px 0 4px; padding: 10px 14px 10px 28px; border-radius: 8px; background: var(--bad-bg); color: var(--bad); }}
    .case.pass .errors {{ background: var(--ok-bg); color: var(--ok); }}
    .ok {{ color: var(--ok); }}
    .sources {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }}
    .source {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fff; }}
    .source.exact {{ border-color: #89c9a8; background: #fbfffc; }}
    .source-title {{ display: flex; justify-content: space-between; gap: 10px; }}
    .source-title span {{ color: var(--muted); white-space: nowrap; }}
    .source p, .empty {{ color: #344054; line-height: 1.45; }}
    @media (max-width: 920px) {{ .stats, .grid {{ grid-template-columns: 1fr; }} .case-head {{ flex-direction: column; }} }}
  </style>
</head>
<body>
  <header>
    <h1>LLM Answer Contract Report</h1>
    <p class="meta">Сформировано: {html.escape(str(report.get("generated_at")))} · root: {html.escape(str(report.get("test_root")))}</p>
  </header>
  <main>
    <section class="stats">
      <div class="stat"><strong>{html.escape(str(summary.get("total_cases", 0)))}</strong><span>кейсов</span></div>
      <div class="stat good"><strong>{html.escape(str(summary.get("passed", 0)))}</strong><span>успешно</span></div>
      <div class="stat bad"><strong>{html.escape(str(summary.get("failed", 0)))}</strong><span>ошибок</span></div>
      <div class="stat good"><strong>{html.escape(str(summary.get("exact_selected_sources", 0)))}</strong><span>точных выбранных источников</span></div>
      <div class="stat"><strong>{html.escape(str(summary.get("avg_answer_chars", 0)))}</strong><span>средняя длина ответа</span></div>
    </section>
    <table>
      <thead><tr><th>Файл</th><th>document_id</th><th>chunks</th><th>Время</th></tr></thead>
      <tbody>{ingestion_rows}</tbody>
    </table>
    <section class="cases">{''.join(case_html)}</section>
  </main>
</body>
</html>
"""


def _answer_contains_cyrillic(answer: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", answer or ""))


def _answer_fact_checks(answer: str, groups: list[list[str]]) -> list[dict[str, Any]]:
    checks = []
    for group in groups:
        checks.append(
            {
                "group": group,
                "matched": any(_contains_term(answer, term) for term in group),
            }
        )
    return checks


def _forbidden_checks(answer: str) -> list[dict[str, Any]]:
    return [{"term": term, "found": _contains_term(answer, term)} for term in FORBIDDEN_ANSWER_TERMS]


class LlmAnswerContractIntegrationTest(unittest.TestCase):
    client: TestClient
    fixture_paths: list[Path]
    report: dict[str, Any]
    recorder: LlmCallRecorder
    _original_get_vectorstores: Any = None
    _original_ask_structured_answer: Any = None
    project_id: int = int(os.getenv("RAG_LLM_CONTRACT_PROJECT_ID", str(int(time.time()) + 100000)))

    @classmethod
    def setUpClass(cls) -> None:
        if os.getenv("RAG_SKIP_LLM_CONTRACT_TESTS", "0") in {"1", "true", "True", "yes", "YES"}:
            raise unittest.SkipTest("RAG_SKIP_LLM_CONTRACT_TESTS is enabled")

        cls.fixture_paths = _fixture_paths()
        missing = [str(path) for path in cls.fixture_paths if not path.exists()]
        if missing:
            raise unittest.SkipTest(
                "PDF fixtures were not found. Set PD_RDMVP_DOC_SEARCH_FIXTURE_DIR or "
                f"PD_RDMVP_DOC_SEARCH_FIXTURES. Missing: {missing}"
            )
        _assert_pdf_runtime_ready()

        cls._original_get_vectorstores = rag_main.get_vectorstores
        cls._original_ask_structured_answer = rag_main.ask_structured_answer
        cls.recorder = LlmCallRecorder(cls._original_ask_structured_answer)
        rag_main.get_vectorstores = fake_get_vectorstores
        rag_main.ask_structured_answer = cls.recorder

        init_db()
        cls.client = TestClient(rag_main.app)
        cls.report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "test_root": str(TEST_ROOT),
            "project_id": cls.project_id,
            "llm_base_url": os.getenv("QWEN_PROXY_BASE_URL", "http://localhost:3264/api"),
            "llm_model": os.getenv("QWEN_MODEL", "qwen3.7-max"),
            "fixtures": [str(path) for path in cls.fixture_paths],
            "ingestions": [],
            "cases": [],
            "summary": {},
        }

        for path in cls.fixture_paths:
            started = time.perf_counter()
            with path.open("rb") as file_obj:
                response = cls.client.post(
                    "/ingest",
                    data={
                        "project_id": str(cls.project_id),
                        "file_type": "pdf",
                        "original_filename": path.name,
                    },
                    files={"file": (path.name, file_obj, "application/pdf")},
                )
            duration_ms = int((time.perf_counter() - started) * 1000)
            payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            cls.report["ingestions"].append(
                {
                    "filename": path.name,
                    "status_code": response.status_code,
                    "document_id": payload.get("document_id"),
                    "chunks_created": payload.get("chunks_created"),
                    "duration_ms": duration_ms,
                    "error": payload.get("detail") if response.status_code >= 400 else None,
                }
            )
            if response.status_code != 200:
                _summarize_report(cls.report)
                _write_report(cls.report)
                raise AssertionError(f"Ingestion failed for {path.name}: {response.status_code} {response.text}")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._original_get_vectorstores is not None:
            rag_main.get_vectorstores = cls._original_get_vectorstores
        if cls._original_ask_structured_answer is not None:
            rag_main.ask_structured_answer = cls._original_ask_structured_answer
        if hasattr(cls, "report"):
            _summarize_report(cls.report)
            _write_report(cls.report)

    def test_01_llm_answer_contract(self) -> None:
        failures: list[str] = []
        for case in LLM_CASES:
            result = self._run_case(case)
            self.report["cases"].append(result)
            if result["status"] != "passed":
                failures.extend(f"{case['id']}: {error}" for error in result["errors"])
        if failures:
            self.fail("\n".join(failures))

    def _run_case(self, case: dict[str, Any]) -> dict[str, Any]:
        started_call_count = len(self.recorder.calls)
        started = time.perf_counter()
        response = self.client.post(
            "/ask",
            json={
                "project_id": self.project_id,
                "question": case["question"],
                "top_k": int(os.getenv("RAG_LLM_CONTRACT_TOP_K", "8")),
            },
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        llm_call = self.recorder.calls[started_call_count] if len(self.recorder.calls) > started_call_count else {}

        answer = str(payload.get("answer") or "")
        sources = payload.get("sources") or []
        selected_chunk_ids = {int(x) for x in payload.get("selected_chunk_ids") or []}
        selected_sources = [src for src in sources if src.get("chunk_id") in selected_chunk_ids]
        exact_sources = _matching_expected_sources(sources, case)
        exact_selected_sources = _matching_expected_sources(selected_sources, case)
        source_text = "\n".join(str(src.get("text") or "") for src in sources)
        fact_checks = _answer_fact_checks(answer, case.get("answer_fact_groups", []))
        forbidden = _forbidden_checks(answer)

        errors: list[str] = []
        if response.status_code != 200:
            errors.append(f"HTTP {response.status_code}: {payload or response.text}")
        if not llm_call:
            errors.append("LLM was not called")
        elif llm_call.get("error"):
            errors.append(f"LLM call failed: {llm_call['error']}")
        if len(answer.strip()) < int(os.getenv("RAG_LLM_ANSWER_MIN_CHARS", "80")):
            errors.append("Answer is too short to be useful")
        if not _answer_contains_cyrillic(answer):
            errors.append("Answer does not look like readable Russian text")
        if not selected_chunk_ids:
            errors.append("No selected_chunk_ids returned after LLM processing")
        if not exact_sources:
            errors.append(
                f"Expected source page(s) {case['expected_pages']} in file containing {case['expected_filename_part']!r}"
            )
        if not exact_selected_sources:
            errors.append(
                "Expected at least one selected source to match both expected file and expected page"
            )
        for check in fact_checks:
            if not check["matched"]:
                errors.append(f"Answer does not contain expected fact group: {check['group']}")
        for check in forbidden:
            if check["found"]:
                errors.append(f"Answer exposes forbidden/internal marker: {check['term']}")
        for term in case.get("source_terms", []):
            if not _contains_term(source_text, term):
                errors.append(f"Returned sources do not contain expected source term: {term!r}")

        return {
            "id": case["id"],
            "question": case["question"],
            "expected_filename_part": case["expected_filename_part"],
            "expected_pages": [int(page) for page in case.get("expected_pages", [])],
            "answer": answer,
            "answer_chars": len(answer),
            "status": "passed" if not errors else "failed",
            "duration_ms": duration_ms,
            "status_code": response.status_code,
            "selected_chunk_ids": sorted(selected_chunk_ids),
            "exact_source": bool(exact_sources),
            "exact_selected_source": bool(exact_selected_sources),
            "fact_checks": fact_checks,
            "forbidden_checks": forbidden,
            "llm_call": llm_call,
            "sources": _source_digest(sources, case),
            "selected_sources": _source_digest(selected_sources, case),
            "errors": errors,
        }


if __name__ == "__main__":
    unittest.main(verbosity=2)
