from __future__ import annotations

from pydantic import BaseModel
from pathlib import Path
import os


class Settings(BaseModel):
    RAG_SERVICE_URL: str = os.getenv("RAG_SERVICE_URL", "http://rag:8001").rstrip("/")
    IFC_SERVICE_URL: str = os.getenv("IFC_SERVICE_URL", "http://ifc:8002").rstrip("/")

    PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
    DATA_DIR: Path = Path(os.getenv("API_DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()
    LOG_DIR: Path = Path(os.getenv("API_LOG_DIR", str(PROJECT_ROOT / "logs"))).resolve()
    TEMP_UPLOADS_DIR: Path = Path(os.getenv("API_TEMP_UPLOADS_DIR", str(DATA_DIR / "tmp_uploads"))).resolve()
    PROJECT_FILES_DIR: Path = Path(os.getenv("API_PROJECT_FILES_DIR", str(DATA_DIR / "project_files"))).resolve()

    SQLITE_PATH: Path = Path(os.getenv("API_SQLITE_PATH", str(DATA_DIR / "api.db"))).resolve()
    SQLALCHEMY_DATABASE_URL: str = os.getenv("API_DATABASE_URL", f"sqlite:///{SQLITE_PATH}")

    CORS_ORIGINS: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")
    ENABLE_METRICS: bool = os.getenv("ENABLE_METRICS", "true").lower() == "true"
    SESSION_TTL_HOURS: int = int(os.getenv("API_SESSION_TTL_HOURS", "168"))
    QWEN_PROXY_BASE_URL: str = os.getenv("QWEN_PROXY_BASE_URL", "http://host.docker.internal:3264/api").rstrip("/")
    QWEN_MODEL: str = os.getenv("QWEN_MODEL", "qwen3.7-max")
    QWEN_TIMEOUT: float = float(os.getenv("QWEN_TIMEOUT", "900"))
    ENABLE_OPTIONAL_OCR: bool = os.getenv("ENABLE_OPTIONAL_OCR", "false").lower() == "true"
    OCR_LANG: str = os.getenv("OCR_LANG", "rus+eng")
    # Промпт 8.2 (CASE10_AGENT_PROMPTS_BACKLOG.md): alternative full-page OCR-fallback
    # engine, switchable without a code change so the tesseract path stays the default
    # until a PaddleOCR pilot proves a statistically significant accuracy gain (see
    # evaluation/OCR_PADDLEOCR_PILOT_REPORT.md) -- "tesseract" and "paddleocr" are the
    # only two recognized values; anything else is treated as "tesseract" by
    # app/domain/dataset_sources.py's dispatch.
    OCR_ENGINE: str = os.getenv("CASE10_OCR_ENGINE", "tesseract").strip().lower()
    # PaddleOCR is one-language-per-pipeline (unlike tesseract's combined "rus+eng"
    # pass) -- "ru" resolves to PP-OCR's `eslav_PP-OCRv5_mobile_rec` model (the
    # ru/be/uk East Slavic group), matching this corpus's actual content (Russian
    # construction documents; see app/domain/ocr_paddle.py for the model-choice
    # rationale and its measured accuracy vs. tesseract on this corpus). NOT
    # "cyrillic" -- that string is a documentation/marketing label for the broader
    # language group, not a valid `lang=` value PaddleOCR 3.7.0 actually accepts
    # (confirmed empirically: raises ValueError "No models are available for
    # lang='cyrillic'"); the real per-language codes are enumerated in
    # `paddleocr._utils.langs.CYRILLIC_LANGS`/`ESLAV_LANGS`.
    OCR_PADDLE_LANG: str = os.getenv("CASE10_OCR_PADDLE_LANG", "ru")
    AUTO_PROCESS_UPLOADS: bool = os.getenv("API_AUTO_PROCESS_UPLOADS", "false").lower() in ("1", "true", "yes")
    # ТЗ 9.1: per-file cap (50 MB) and per-package cap (200 MB, summed across the
    # files uploaded into the same still-open verification cycle for a project).
    MAX_UPLOAD_FILE_SIZE_BYTES: int = int(os.getenv("API_MAX_UPLOAD_FILE_SIZE_BYTES", str(50 * 1024 * 1024)))
    MAX_UPLOAD_PACKAGE_SIZE_BYTES: int = int(os.getenv("API_MAX_UPLOAD_PACKAGE_SIZE_BYTES", str(200 * 1024 * 1024)))
    RABBITMQ_URL: str = os.getenv("RABBITMQ_URL", "").strip()
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/2").strip()
    CASE10_JOB_MAX_ATTEMPTS: int = int(os.getenv("CASE10_JOB_MAX_ATTEMPTS", "3"))
    CASE10_JOB_STALE_SECONDS: int = int(os.getenv("CASE10_JOB_STALE_SECONDS", "900"))
    CASE10_WORKER_RECONNECT_SECONDS: float = float(os.getenv("CASE10_WORKER_RECONNECT_SECONDS", "5"))

    # Bounded fallback discovery (used only when the fast, upstream-tag-based path
    # finds no evidence at all for a required parameter/stage): caps how many
    # documents and pages it is allowed to open, so an evidence gap on one object
    # can never turn into an unbounded scan of the whole corpus.
    RULE_FALLBACK_MAX_DOCUMENTS: int = int(os.getenv("RULE_FALLBACK_MAX_DOCUMENTS", "8"))
    RULE_FALLBACK_MAX_PAGES_TOTAL: int = int(os.getenv("RULE_FALLBACK_MAX_PAGES_TOTAL", "160"))
    RULE_FALLBACK_MAX_OCR_PAGES: int = int(os.getenv("RULE_FALLBACK_MAX_OCR_PAGES", "5"))
    # A single (parameter, stage, location) gap may draw at most this many pages
    # from the shared pool above, so one unlucky/expensive gap early in a process
    # run cannot exhaust the whole budget and starve every other gap after it.
    RULE_FALLBACK_MAX_PAGES_PER_ATTEMPT: int = int(os.getenv("RULE_FALLBACK_MAX_PAGES_PER_ATTEMPT", "60"))

    # Live candidate tagger (ТЗ 9.1 item 2 -- see live_candidate_tagger.py): scans
    # a document's own pages for matrix-parameter anchor phrases so an object
    # with no organizer-supplied learning_annotation training data can still feed
    # the extraction mechanisms. Idempotent per document (a scanned document
    # carries a `live_tagger_scan` marker and is skipped on a later run), so the
    # scan is a ONE-TIME cost per document -- it does not compete with the ТЗ 11
    # "<=2 minutes per comparison run" budget once a document has been tagged.
    #
    # LIMITS ARE DETERMINISTIC (documents/pages), NEVER WALL-CLOCK. The former
    # `CASE10_LIVE_TAGGER_MAX_SECONDS=600` made the set of scanned documents --
    # and so the whole submission -- depend on machine speed/load (on a noisy dev
    # box LOS3A got 65/103 RD documents scanned and PZ-004 regressed; unbounded,
    # 451 fragments/10 groups), contradicting the ТЗ's reproducibility
    # requirement. The three caps below decide WHICH documents get scanned as a
    # pure function of the object (see live_candidate_tagger.py's plan phase:
    # a total order over documents, budget consumed in that order before any
    # work starts), so they can never bite differently on two runs.
    #
    # They are safety valves sized so no known object hits them, not throttles:
    # the largest real object in this corpus (DOO25) has 1750 files, of which
    # 1294 are PDFs totalling ~17k pages (~14.9k after the per-document cap);
    # non-PDF files (docx/rar/...) can never yield a page and are excluded from
    # the plan, not charged against MAX_DOCUMENTS (the old 500-document ceiling
    # was also being spent on them). Cost at the ceiling: pages are ~10-12/sec
    # per core on dense drawing-sheet PDFs (the pessimistic case, measured on the
    # 8-core dev machine, see evaluation/LIVE_TAGGER_SCALE_DETERMINISM_REPORT.md),
    # so 30000 pages = ~45 min of single-core time = ~3-4 min spread over the
    # grading server's 24 physical cores.
    LIVE_TAGGER_ENABLED: bool = os.getenv("CASE10_LIVE_TAGGER_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    LIVE_TAGGER_MAX_DOCUMENTS: int = int(os.getenv("CASE10_LIVE_TAGGER_MAX_DOCUMENTS", "3000"))
    LIVE_TAGGER_MAX_PAGES_TOTAL: int = int(os.getenv("CASE10_LIVE_TAGGER_MAX_PAGES_TOTAL", "30000"))
    LIVE_TAGGER_MAX_PAGES_PER_DOCUMENT: int = int(os.getenv("CASE10_LIVE_TAGGER_MAX_PAGES_PER_DOCUMENT", "220"))
    # Parallelism (does not change WHAT is scanned or the merged output, only how
    # fast): a spawn-based process pool, one task per document. 0 = auto = the
    # CPUs actually available to this process (affinity/cgroup quota aware),
    # capped by LIVE_TAGGER_MAX_WORKERS (the grading server's 24 physical cores;
    # its 48 hardware threads add little for this CPU-bound work). An explicit
    # positive value is used as-is (1 = fully in-process, no pool). In auto mode
    # a worker is only started per LIVE_TAGGER_MIN_PAGES_PER_WORKER pages of
    # real (uncached) work, so a small object never pays worker start-up cost.
    LIVE_TAGGER_WORKERS: int = int(os.getenv("CASE10_LIVE_TAGGER_WORKERS", "0"))
    LIVE_TAGGER_MAX_WORKERS: int = int(os.getenv("CASE10_LIVE_TAGGER_MAX_WORKERS", "24"))
    LIVE_TAGGER_MIN_PAGES_PER_WORKER: int = int(os.getenv("CASE10_LIVE_TAGGER_MIN_PAGES_PER_WORKER", "100"))
    # On-disk result cache, content-addressed by the document's SHA-256 (plus the
    # anchor set / scan settings / scan-code fingerprint -- see
    # live_tagger_scan.py), so a re-run of the same image/volume re-tags nothing.
    LIVE_TAGGER_CACHE_ENABLED: bool = os.getenv("CASE10_LIVE_TAGGER_CACHE_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    LIVE_TAGGER_CACHE_DIR: Path = Path(os.getenv("CASE10_LIVE_TAGGER_CACHE_DIR", str(DATA_DIR / "live_tagger_cache"))).resolve()

    # ТЗ 9.6: integration with the external ИАИС "РиН" system (Low priority).
    # Base URL of the external system; POST {IAIS_RIN_BASE_URL}/api/v1/inspection/{process_id}
    # is called once a protocol is finalized. Empty by default -- no real target
    # is deployed for this hackathon -- which the client treats as "sync
    # disabled" rather than attempting a request against an empty URL.
    IAIS_RIN_BASE_URL: str = os.getenv("IAIS_RIN_BASE_URL", "").rstrip("/")
    IAIS_RIN_TIMEOUT_SECONDS: float = float(os.getenv("IAIS_RIN_TIMEOUT_SECONDS", "30"))
    # УКЭП (усиленная квалифицированная электронная подпись): ТЗ 12.10 requires
    # requests to ИАИС РИН to be signed with it. This codebase has no certified
    # УКЭП signing provider available, so this is limited to configuring the
    # path to a client TLS certificate (mutual TLS) for whatever cert a real
    # deployment provisions -- documented as a known gap, not implemented as a
    # full УКЭП signature. See `iais_rin_client.py` docstring.
    IAIS_RIN_CLIENT_CERT_PATH: str = os.getenv("IAIS_RIN_CLIENT_CERT_PATH", "").strip()
    IAIS_RIN_CLIENT_KEY_PATH: str = os.getenv("IAIS_RIN_CLIENT_KEY_PATH", "").strip()
    # ТЗ 9.6: "до 3 повторных попыток с экспоненциальной задержкой (1, 5, 15 минут)",
    # then hourly while the outage persists (PENDING_SYNC).
    IAIS_RIN_RETRY_DELAYS_SECONDS: tuple[int, ...] = (60, 300, 900)
    IAIS_RIN_PENDING_SYNC_RETRY_SECONDS: int = int(os.getenv("IAIS_RIN_PENDING_SYNC_RETRY_SECONDS", str(60 * 60)))


settings = Settings()
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.LOG_DIR.mkdir(parents=True, exist_ok=True)

settings.TEMP_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
settings.PROJECT_FILES_DIR.mkdir(parents=True, exist_ok=True)
