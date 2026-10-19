from __future__ import annotations

import asyncio
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from .logging_setup import setup_logging
import logging
from .config import settings
from .request_context import RequestIDMiddleware
from .db.session import init_db
from .api.routes_projects import router as projects_router
from .api.routes_upload import router as upload_router
from .api.routes_history import router as history_router
from .api.routes_documents import router as documents_router
from .api.routes_admin import router as admin_router
from .api.routes_me import router as me_router
from .api.routes_auth import router as auth_router
from .api.routes_case10 import router as case10_router
from .api.routes_batch import router as batch_router


setup_logging("api")
logger = logging.getLogger("api")
_weekly_report_task: asyncio.Task | None = None

app = FastAPI(title="API Service", version="2.1.0")

# CORS
origins = [o.strip() for o in settings.CORS_ORIGINS if o.strip()]
if origins == ["*"]:
    allow_origins = ["*"]
else:
    allow_origins = origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Opt-in (CASE10_API_GZIP=1): gzip for JSON responses of >= 100 KB (protocols/current is ~2.6 MB). Off by default
# because the Node gateway re-sends the decoded body with the upstream Content-Encoding header (see json_gzip.py).
if os.getenv("CASE10_API_GZIP", "0").strip().lower() in ("1", "true", "yes", "on"):
    from .json_gzip import LargeJsonGzipMiddleware

    app.add_middleware(LargeJsonGzipMiddleware, min_bytes=int(os.getenv("CASE10_API_GZIP_MIN_BYTES", str(100 * 1024))))
# Added after CORS so it wraps outside it (Starlette prepends each new
# middleware), meaning every request -- including ones CORS itself rejects --
# still gets a request id.
app.add_middleware(RequestIDMiddleware)


@app.exception_handler(Exception)
async def api_unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled API error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": str(exc) or exc.__class__.__name__,
            "error_type": exc.__class__.__name__,
        },
    )


@app.on_event("startup")
def _startup():
    init_db()
    from .domain.dataset_sources import check_tesseract_health
    from .domain.gpu import detect_gpu

    ocr_health = check_tesseract_health()
    if not ocr_health["available"]:
        logger.error("OCR fallback (tesseract) unavailable at startup: %s", ocr_health.get("error"))

    detect_gpu()

    if settings.OCR_ENGINE == "paddleocr":
        from .domain.ocr_paddle import check_paddleocr_health

        paddle_health = check_paddleocr_health()
        if not paddle_health["available"]:
            logger.error(
                "CASE10_OCR_ENGINE=paddleocr but the PaddleOCR pipeline failed to load at "
                "startup (calls will fall back to tesseract): %s", paddle_health.get("error"),
            )


@app.on_event("startup")
async def _start_weekly_report_scheduler():
    global _weekly_report_task
    if settings.ML_WEEKLY_REPORT_SCHEDULER_ENABLED and _weekly_report_task is None:
        from .domain.ml_weekly_scheduler import run_scheduler
        _weekly_report_task = asyncio.create_task(run_scheduler(), name="case10-weekly-ml-report")


@app.on_event("shutdown")
async def _stop_weekly_report_scheduler():
    global _weekly_report_task
    if _weekly_report_task is not None:
        _weekly_report_task.cancel()
        try:
            await _weekly_report_task
        except asyncio.CancelledError:
            pass
        _weekly_report_task = None


@app.get("/health")
def health():
    from .domain.dataset_sources import tesseract_health_status
    from .domain.gpu import gpu_health_status

    payload = {"status": "ok", "ocr_tesseract": tesseract_health_status(), "gpu": gpu_health_status()}
    if settings.OCR_ENGINE == "paddleocr":
        from .domain.ocr_paddle import paddleocr_health_status

        payload["ocr_paddleocr"] = paddleocr_health_status()
    return payload


# Metrics (optional)
if settings.ENABLE_METRICS:
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST  # type: ignore

    @app.get("/metrics")
    def metrics():
        data = generate_latest()
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)


app.include_router(auth_router, prefix="/api")
app.include_router(me_router, prefix="/api")
app.include_router(projects_router, prefix="/api")
app.include_router(upload_router, prefix="/api")
app.include_router(history_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(case10_router, prefix="/api")
app.include_router(batch_router, prefix="/api")
app.include_router(admin_router, prefix="/api")

# Legacy DocuRAG features that depend on separate RAG/IFC services (chat and RAG page/region assets). The offline
# CASE10 delivery (CASE10_OFFLINE_DELIVERY=1) neither imports nor mounts them -- expert session §10: project
# documents must not leave the circuit.
if not settings.OFFLINE_DELIVERY:
    from .api.routes_chat import router as chat_router
    from .api.routes_rag_assets import router as rag_assets_router

    app.include_router(chat_router, prefix="/api")
    app.include_router(rag_assets_router, prefix="/api")
