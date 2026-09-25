from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEST_ROOT = Path(os.getenv("RAG_SEARCH_TEST_ROOT", tempfile.mkdtemp(prefix="rag-search-it-"))).resolve()
os.environ.setdefault("RAG_DATA_DIR", str(TEST_ROOT / "data"))
os.environ.setdefault("RAG_UPLOADS_DIR", str(TEST_ROOT / "data" / "uploads"))
os.environ.setdefault("RAG_VECTORSTORE_DIR", str(TEST_ROOT / "data" / "vectorstore"))
os.environ.setdefault("RAG_PAGES_DIR", str(TEST_ROOT / "data" / "pages"))
os.environ.setdefault("RAG_REGIONS_DIR", str(TEST_ROOT / "data" / "regions"))
os.environ.setdefault("RAG_LOG_DIR", str(TEST_ROOT / "logs"))
os.environ.setdefault("RAG_SQLITE_PATH", str(TEST_ROOT / "data" / "rag.db"))
os.environ.setdefault("RAG_CHUNK_SIZE", "1800")
os.environ.setdefault("RAG_CHUNK_OVERLAP", "240")
os.environ.setdefault("RAG_ENABLE_OCR", "0")

from fastapi.testclient import TestClient  # noqa: E402

from rag_service.app.db.session import init_db  # noqa: E402
from rag_service.app import main as rag_main  # noqa: E402


REPORT_DIR = Path(
    os.getenv(
        "RAG_SEARCH_REPORT_DIR",
        str(PROJECT_ROOT / "rag_service" / "test_reports" / "document_search"),
    )
).resolve()

DEFAULT_FIXTURE_DIR = Path.home() / "Downloads" / "Telegram Desktop"
DEFAULT_FIXTURE_NAMES = [
    "318-ПБ-Р-К2-2023-КЖ1.24.pdf",
    "3-18-ПБ-Р-К2-2023-КМ1.pdf",
    "318-ПБ-Р-К2-2023-АР3.pdf",
]

SEARCH_CASES: list[dict[str, Any]] = [
    {
        "id": "reinforced_stair_flight",
        "question": "Найди армирование Лм-9.6 лестничного марша",
        "expected_filename_part": "КЖ1.24",
        "expected_pages": [8],
        "expected_terms": ["Лм-9.6", "Армирование"],
    },
    {
        "id": "embedded_part_m1",
        "question": "Где описана закладная деталь М-1 для сборного марша?",
        "expected_filename_part": "КМ1",
        "expected_pages": [5],
        "expected_terms": ["Закладная деталь М-1", "сборного марша"],
    },
    {
        "id": "engineering_shafts_masonry",
        "question": "Что сказано про кладку инженерных шахт?",
        "expected_filename_part": "АР3",
        "expected_pages": [7],
        "expected_terms": ["Кладку инженерных шахт", "инженерных коммуникаций"],
    },
]


class FakeVectorStores:
    def __init__(self) -> None:
        self.text_upserts = 0
        self.asset_upserts = 0

    def upsert_text(self, *, ids: list[str], texts: list[str], metadatas: list[dict[str, Any]]) -> None:
        del texts, metadatas
        self.text_upserts += len(ids)

    def upsert_asset(self, *, ids: list[str], texts: list[str], metadatas: list[dict[str, Any]]) -> None:
        del texts, metadatas
        self.asset_upserts += len(ids)

    def query_text(self, *, query_text: str, top_k: int, where: dict[str, Any]) -> list[dict[str, Any]]:
        del query_text, top_k, where
        return []

    def query_asset(self, *, query_text: str, top_k: int, where: dict[str, Any]) -> list[dict[str, Any]]:
        del query_text, top_k, where
        return []

    def delete_text(self, ids: list[str]) -> int:
        return len(ids)

    def delete_asset(self, ids: list[str]) -> int:
        return len(ids)

    def delete_text_where(self, where: dict[str, Any]) -> None:
        del where

    def delete_asset_where(self, where: dict[str, Any]) -> None:
        del where


FAKE_VECTORSTORES = FakeVectorStores()


def fake_get_vectorstores() -> FakeVectorStores:
    return FAKE_VECTORSTORES


def fake_ask_structured_answer(
    *,
    question: str,
    chunks: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    system_prompt: str,
    model: str,
) -> tuple[str, list[int], list[int], str]:
    del assets, system_prompt, model
    selected = [int(chunk["chunk_id"]) for chunk in chunks[:3] if chunk.get("chunk_id") is not None]
    first = chunks[0] if chunks else {}
    excerpt = re.sub(r"\s+", " ", str(first.get("text") or "")).strip()[:500]
    filename = first.get("filename") or "document"
    page = first.get("page_number") or 0
    answer = f"Тестовый ответ для запроса: {question}\nИсточник: {filename}, лист {page}.\nФрагмент: {excerpt}"
    return answer, selected, [], json.dumps({"mode": "deterministic-test"}, ensure_ascii=False)


def _fixture_paths() -> list[Path]:
    explicit = os.getenv("PD_RDMVP_DOC_SEARCH_FIXTURES", "").strip()
    if explicit:
        return [Path(part.strip()).expanduser().resolve() for part in explicit.split("|") if part.strip()]

    fixture_dir = Path(os.getenv("PD_RDMVP_DOC_SEARCH_FIXTURE_DIR", str(DEFAULT_FIXTURE_DIR))).expanduser()
    return [(fixture_dir / name).resolve() for name in DEFAULT_FIXTURE_NAMES]


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _contains_term(haystack: str, term: str) -> bool:
    return _norm(term) in _norm(haystack)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _matches_expected_file(source: dict[str, Any], expected_filename_part: str) -> bool:
    return expected_filename_part in str(source.get("filename") or "")


def _matching_expected_sources(sources: list[dict[str, Any]], case: dict[str, Any]) -> list[dict[str, Any]]:
    expected_pages = {int(page) for page in case.get("expected_pages", [])}
    expected_filename_part = str(case.get("expected_filename_part") or "")
    matches: list[dict[str, Any]] = []
    for source in sources:
        page_number = _as_int(source.get("page_number"))
        if _matches_expected_file(source, expected_filename_part) and page_number in expected_pages:
            matches.append(source)
    return matches


def _assert_pdf_runtime_ready() -> None:
    missing: list[str] = []
    try:
        import fitz  # noqa: F401
    except Exception as exc:
        missing.append(f"PyMuPDF/module fitz ({exc.__class__.__name__}: {exc})")

    if not missing:
        return

    message = (
        "PDF ingestion runtime is not ready. Missing: "
        + "; ".join(missing)
        + ". Install RAG dependencies before running this integration test: "
        + "python -m pip install -r rag_service/requirements.txt"
    )
    raise AssertionError(message)


def _source_digest(sources: list[dict[str, Any]], case: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    digest = []
    expected_filename_part = str((case or {}).get("expected_filename_part") or "")
    expected_pages = {int(page) for page in (case or {}).get("expected_pages", [])}
    for item in sources[:8]:
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        page_number = _as_int(item.get("page_number"))
        filename_match = bool(expected_filename_part and _matches_expected_file(item, expected_filename_part))
        page_match = bool(page_number in expected_pages) if expected_pages else False
        digest.append(
            {
                "filename": item.get("filename"),
                "page_number": page_number,
                "chunk_id": item.get("chunk_id"),
                "score": item.get("score"),
                "filename_match": filename_match,
                "page_match": page_match,
                "exact_match": filename_match and page_match,
                "text_preview": text[:500],
            }
        )
    return digest


def _write_report(report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / "document_search_report.json"
    html_path = REPORT_DIR / "index.html"
    report["report_files"] = {"json": str(json_path), "html": str(html_path)}
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(_render_report_html(report), encoding="utf-8")


def _summarize_report(report: dict[str, Any]) -> None:
    cases = report.get("cases", [])
    passed = sum(1 for case in cases if case.get("status") == "passed")
    failed = sum(1 for case in cases if case.get("status") != "passed")
    ingestion_failures = sum(1 for item in report.get("ingestions", []) if item.get("status_code") != 200)
    exact_page_matches = sum(1 for case in cases if case.get("exact_page_match"))
    report["summary"] = {
        "total_cases": len(cases),
        "passed": passed,
        "failed": failed,
        "exact_page_matches": exact_page_matches,
        "ingestion_failures": ingestion_failures,
        "total_sources": sum(int(case.get("sources_count") or 0) for case in cases),
        "text_vectors_stubbed": FAKE_VECTORSTORES.text_upserts,
    }


def _render_report_html(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    cases = report.get("cases", [])
    ingestions = report.get("ingestions", [])

    def chips(values: list[Any], css_class: str = "") -> str:
        if not values:
            return '<span class="chip muted-chip">нет</span>'
        return "".join(f'<span class="chip {css_class}">{html.escape(str(value))}</span>' for value in values)

    def source_cards(items: list[dict[str, Any]]) -> str:
        if not items:
            return '<p class="empty">Нет источников для отображения.</p>'
        out = []
        for src in items:
            exact = bool(src.get("exact_match"))
            css = "source-card exact" if exact else "source-card"
            badge = "точная страница" if exact else "источник"
            out.append(
                f"""
                <article class="{css}">
                  <div class="source-top">
                    <strong>{html.escape(str(src.get('filename')))}</strong>
                    <span>{html.escape(badge)}</span>
                  </div>
                  <div class="source-meta">
                    <span>лист {html.escape(str(src.get('page_number')))}</span>
                    <span>chunk {html.escape(str(src.get('chunk_id')))}</span>
                    <span>score {html.escape(str(src.get('score')))}</span>
                  </div>
                  <p>{html.escape(str(src.get('text_preview') or ''))}</p>
                </article>
                """
            )
        return "".join(out)

    case_rows = []
    for case in cases:
        status = html.escape(str(case.get("status", "unknown")))
        css = "pass" if case.get("status") == "passed" else "fail"
        exact_sources = source_cards(case.get("exact_sources", []))
        sources = source_cards(case.get("sources", []))
        errors = "".join(f"<li>{html.escape(str(err))}</li>" for err in case.get("errors", []))
        if not errors:
            errors = '<li class="ok-line">Ошибок нет.</li>'
        case_rows.append(
            f"""
            <article class="case {css}">
              <div class="case-head">
                <div>
                  <h2>{html.escape(str(case.get("id")))}</h2>
                  <p>{html.escape(str(case.get("question")))}</p>
                </div>
                <span class="status">{status}</span>
              </div>
              <div class="case-grid">
                <section>
                  <h3>Ожидание</h3>
                  <dl>
                    <dt>Файл</dt><dd>{html.escape(str(case.get("expected_filename_part")))}</dd>
                    <dt>Страницы</dt><dd>{chips(case.get("expected_pages", []), "expect-chip")}</dd>
                    <dt>Термины</dt><dd>{chips(case.get("expected_terms", []), "term-chip")}</dd>
                  </dl>
                </section>
                <section>
                  <h3>Результат</h3>
                  <dl>
                    <dt>Страницы найденного файла</dt><dd>{chips(case.get("found_pages_for_expected_file", []), "found-chip")}</dd>
                    <dt>Точных совпадений</dt><dd>{html.escape(str(case.get("exact_match_count", 0)))}</dd>
                    <dt>Время / источники</dt><dd>{html.escape(str(case.get("duration_ms")))} ms / {html.escape(str(case.get("sources_count")))}</dd>
                  </dl>
                </section>
              </div>
              <ul class="errors">{errors}</ul>
              <h3>Точные совпадения по файлу и странице</h3>
              <div class="sources">{exact_sources}</div>
              <h3>Все источники из ответа</h3>
              <div class="sources">{sources}</div>
            </article>
            """
        )

    ingest_rows = "".join(
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
  <title>Document Search Integration Report</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #182230;
      --muted: #667085;
      --line: #d0d7e2;
      --ok: #14714b;
      --ok-bg: #eaf7ef;
      --bad: #b42318;
      --bad-bg: #fff0ed;
      --warn-bg: #fff7df;
      --surface: #f4f7fb;
      --panel: #ffffff;
      --chip: #edf2f7;
    }}
    body {{
      margin: 0;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      color: var(--ink);
      background: var(--surface);
    }}
    header {{
      padding: 30px 32px 22px;
      background: linear-gradient(180deg, #ffffff 0%, #eef4fb 100%);
      border-bottom: 1px solid var(--line);
    }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 28px; }}
    h3 {{ font-size: 14px; margin: 18px 0 10px; color: #344054; }}
    main {{ padding: 24px 32px 40px; max-width: 1240px; margin: 0 auto; }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(5, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }}
    .stat, .case, table {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }}
    .stat {{ padding: 16px; border-left: 4px solid #7a8fa8; }}
    .stat.good {{ border-left-color: var(--ok); }}
    .stat.bad {{ border-left-color: var(--bad); }}
    .stat.info {{ border-left-color: #2563a8; }}
    .stat strong {{ display: block; font-size: 28px; }}
    .stat span, .meta {{ color: var(--muted); font-size: 13px; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; overflow: hidden; }}
    th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--line); }}
    th {{ font-size: 13px; color: var(--muted); }}
    tr:last-child td {{ border-bottom: 0; }}
    .cases {{ display: grid; gap: 16px; }}
    .case {{ padding: 20px; }}
    .case.pass {{ border-left: 5px solid var(--ok); }}
    .case.fail {{ border-left: 5px solid var(--bad); }}
    .case-head {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; }}
    .case-head p {{ margin: 6px 0 0; color: #344054; }}
    .status {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      font-weight: 700;
      background: var(--chip);
      white-space: nowrap;
    }}
    .case.pass .status {{ color: var(--ok); background: var(--ok-bg); }}
    .case.fail .status {{ color: var(--bad); background: var(--bad-bg); }}
    .case-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      margin-top: 16px;
    }}
    .case-grid section {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #fbfcfe;
    }}
    dl {{ display: grid; grid-template-columns: 180px 1fr; gap: 8px 12px; margin: 0; }}
    dt {{ color: var(--muted); }}
    dd {{ margin: 0; }}
    .chip {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      margin: 0 4px 4px 0;
      border-radius: 999px;
      background: var(--chip);
      border: 1px solid #d9e2ec;
      font-size: 12px;
      font-weight: 650;
    }}
    .expect-chip {{ background: var(--warn-bg); }}
    .found-chip {{ background: #e8f3ff; }}
    .term-chip {{ background: #f1f5f9; }}
    .muted-chip {{ color: var(--muted); }}
    .errors {{
      margin: 16px 0 4px;
      padding: 10px 14px 10px 28px;
      border-radius: 8px;
      color: var(--bad);
      background: var(--bad-bg);
    }}
    .ok-line {{ color: var(--ok); }}
    .case.pass .errors {{ background: var(--ok-bg); color: var(--ok); }}
    .sources {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
    }}
    .source-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #ffffff;
    }}
    .source-card.exact {{ border-color: #89c9a8; background: #fbfffc; }}
    .source-top, .source-meta {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
    }}
    .source-top span {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .source-meta {{
      justify-content: flex-start;
      flex-wrap: wrap;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .source-card p, .empty {{ margin: 10px 0 0; color: #344054; line-height: 1.45; }}
    @media (max-width: 820px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      .stats, .case-grid {{ grid-template-columns: 1fr; }}
      dl {{ grid-template-columns: 1fr; }}
      .case-head {{ align-items: flex-start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Document Search Integration Report</h1>
    <p class="meta">Сформировано: {html.escape(str(report.get("generated_at")))} · root: {html.escape(str(report.get("test_root")))}</p>
  </header>
  <main>
    <section class="stats">
      <div class="stat info"><strong>{html.escape(str(summary.get("total_cases", 0)))}</strong><span>кейсов</span></div>
      <div class="stat good"><strong>{html.escape(str(summary.get("passed", 0)))}</strong><span>успешно</span></div>
      <div class="stat bad"><strong>{html.escape(str(summary.get("failed", 0)))}</strong><span>ошибок</span></div>
      <div class="stat good"><strong>{html.escape(str(summary.get("exact_page_matches", 0)))}</strong><span>точных страниц</span></div>
      <div class="stat info"><strong>{html.escape(str(summary.get("total_sources", 0)))}</strong><span>источников найдено</span></div>
    </section>
    <table>
      <thead><tr><th>Файл</th><th>document_id</th><th>chunks</th><th>Время</th></tr></thead>
      <tbody>{ingest_rows}</tbody>
    </table>
    <section class="cases">{''.join(case_rows)}</section>
  </main>
</body>
</html>
"""


class DocumentationSearchIntegrationTest(unittest.TestCase):
    client: TestClient
    fixture_paths: list[Path]
    report: dict[str, Any]
    _original_get_vectorstores: Any = None
    _original_ask_structured_answer: Any = None
    project_id: int = int(os.getenv("RAG_SEARCH_PROJECT_ID", str(int(time.time()))))

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_paths = _fixture_paths()
        missing = [str(path) for path in cls.fixture_paths if not path.exists()]
        if missing:
            missing_report = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "test_root": str(TEST_ROOT),
                "project_id": cls.project_id,
                "fixtures": [str(path) for path in cls.fixture_paths],
                "ingestions": [],
                "cases": [],
                "summary": {"total_cases": 0, "passed": 0, "failed": 0, "ingestion_failures": 0},
                "errors": [f"Missing fixture: {path}" for path in missing],
            }
            _write_report(missing_report)
            raise unittest.SkipTest(
                "PDF fixtures were not found. Set PD_RDMVP_DOC_SEARCH_FIXTURE_DIR or "
                f"PD_RDMVP_DOC_SEARCH_FIXTURES. Missing: {missing}"
            )

        try:
            _assert_pdf_runtime_ready()
        except AssertionError as exc:
            dependency_report = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "test_root": str(TEST_ROOT),
                "project_id": cls.project_id,
                "fixtures": [str(path) for path in cls.fixture_paths],
                "ingestions": [],
                "cases": [],
                "summary": {"total_cases": 0, "passed": 0, "failed": 0, "ingestion_failures": 0},
                "errors": [str(exc)],
            }
            _write_report(dependency_report)
            raise

        cls._original_get_vectorstores = rag_main.get_vectorstores
        cls._original_ask_structured_answer = rag_main.ask_structured_answer
        rag_main.get_vectorstores = fake_get_vectorstores
        rag_main.ask_structured_answer = fake_ask_structured_answer

        init_db()
        cls.client = TestClient(rag_main.app)
        cls.report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "test_root": str(TEST_ROOT),
            "project_id": cls.project_id,
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
            if int(payload.get("chunks_created") or 0) <= 0:
                _summarize_report(cls.report)
                _write_report(cls.report)
                raise AssertionError(f"Ingestion created no chunks for {path.name}: {payload}")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._original_get_vectorstores is not None:
            rag_main.get_vectorstores = cls._original_get_vectorstores
        if cls._original_ask_structured_answer is not None:
            rag_main.ask_structured_answer = cls._original_ask_structured_answer
        if hasattr(cls, "report"):
            _summarize_report(cls.report)
            _write_report(cls.report)

    def test_01_ingests_all_pdf_fixtures(self) -> None:
        self.assertEqual(len(self.report["ingestions"]), len(self.fixture_paths))
        for item in self.report["ingestions"]:
            self.assertEqual(item["status_code"], 200, item)
            self.assertGreater(int(item["chunks_created"] or 0), 0, item)

    def test_02_search_returns_expected_document_sources(self) -> None:
        failures: list[str] = []
        for case in SEARCH_CASES:
            result = self._run_search_case(case)
            self.report["cases"].append(result)
            if result["status"] != "passed":
                failures.extend(f"{case['id']}: {error}" for error in result["errors"])
        if failures:
            self.fail("\n".join(failures))

    def _run_search_case(self, case: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        errors: list[str] = []
        response = self.client.post(
            "/ask",
            json={
                "project_id": self.project_id,
                "question": case["question"],
                "top_k": 8,
            },
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        sources = payload.get("sources") or []
        source_text = "\n".join(str(src.get("text") or "") for src in sources)
        source_filenames = [str(src.get("filename") or "") for src in sources]
        source_pages = sorted(
            {
                _as_int(src.get("page_number"))
                for src in sources
                if _matches_expected_file(src, str(case["expected_filename_part"]))
            }
            - {None}
        )
        expected_pages = [int(page) for page in case.get("expected_pages", [])]
        exact_page_sources = _matching_expected_sources(sources, case)

        if response.status_code != 200:
            errors.append(f"HTTP {response.status_code}: {payload or response.text}")
        if not sources:
            errors.append("No sources returned by /ask")
        if not any(case["expected_filename_part"] in filename for filename in source_filenames):
            errors.append(
                f"Expected source filename containing {case['expected_filename_part']!r}, got {source_filenames}"
            )
        if expected_pages and not exact_page_sources:
            errors.append(
                f"Expected source page(s) {expected_pages} in file containing "
                f"{case['expected_filename_part']!r}, got pages {source_pages}"
            )
        for term in case["expected_terms"]:
            if not _contains_term(source_text, term):
                errors.append(f"Expected term {term!r} was not found in returned source text")

        return {
            "id": case["id"],
            "question": case["question"],
            "expected_filename_part": case["expected_filename_part"],
            "expected_pages": expected_pages,
            "expected_terms": case["expected_terms"],
            "status": "passed" if not errors else "failed",
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "sources_count": len(sources),
            "found_pages_for_expected_file": source_pages,
            "exact_page_match": bool(exact_page_sources),
            "exact_match_count": len(exact_page_sources),
            "selected_chunk_ids": payload.get("selected_chunk_ids") or [],
            "pages": payload.get("pages") or [],
            "sources": _source_digest(sources, case),
            "exact_sources": _source_digest(exact_page_sources, case),
            "errors": errors,
        }


if __name__ == "__main__":
    unittest.main(verbosity=2)
