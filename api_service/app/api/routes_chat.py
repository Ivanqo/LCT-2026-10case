from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from shared.llm import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, QwenProxyClient

from ..clients.ifc_client import IfcClient
from ..clients.rag_client import RagClient
from ..db.models import QASession, User
from ..db.session import SessionLocal, get_db
from ..schemas import (
    ChatRequest,
    ChatResponse,
    IfcSourceRef,
    PageViewerRef,
    SourceAssetRef,
    SourceRef,
)
from .auth import get_current_user
from .utils import get_project_for_org

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)

CHAT_JOBS: dict[str, dict[str, Any]] = {}
CHAT_JOBS_LOCK = asyncio.Lock()
CHAT_JOB_TTL_SECONDS = 60 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _cleanup_chat_jobs() -> None:
    now = datetime.now(timezone.utc).timestamp()
    async with CHAT_JOBS_LOCK:
        stale = []
        for job_id, job in CHAT_JOBS.items():
            created_ts = float(job.get("created_ts") or now)
            if now - created_ts > CHAT_JOB_TTL_SECONDS:
                stale.append(job_id)
        for job_id in stale:
            CHAT_JOBS.pop(job_id, None)


def _extract_llm_text(response: Any) -> str:
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        for key in ("answer", "text", "content", "response"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(response).strip()


def _should_try_chat_endpoint(exc: Exception) -> bool:
    text = str(exc).lower()
    legacy_markers = (
        "404 client error",
        "405 client error",
        "404 not found",
        "405 method not allowed",
        "cannot post",
        "method not allowed",
    )
    return any(marker in text for marker in legacy_markers)


def _build_ifc_prompt(question: str, rag_answer: str, ifc_context: str) -> str:
    return (
        "Ниже есть ответ по проектной документации и контекст из IFC-модели. "
        "Собери единый ответ инженеру на русском языке. Не выдумывай данные. "
        "Если IFC-контекст не отвечает на вопрос, так и напиши.\n\n"
        f"ВОПРОС:\n{question}\n\n"
        f"ОТВЕТ ПО RAG-ДОКУМЕНТАМ:\n{rag_answer or 'нет данных'}\n\n"
        f"IFC-КОНТЕКСТ:\n{ifc_context or 'нет данных'}"
    )


def _call_llm_for_merged_answer(question: str, rag_answer: str, ifc_context: str) -> str:
    client = QwenProxyClient(
        base_url=os.getenv("QWEN_PROXY_BASE_URL", DEFAULT_BASE_URL),
        timeout=float(os.getenv("QWEN_TIMEOUT", str(DEFAULT_TIMEOUT))),
    )
    messages = [
        {
            "role": "system",
            "content": "Ты профессиональный BIM/RAG ассистент для проектной документации.",
        },
        {"role": "user", "content": _build_ifc_prompt(question, rag_answer, ifc_context)},
    ]
    try:
        response, _ = client.chat_completions(
            model=os.getenv("QWEN_MODEL", "qwen3.7-max"),
            messages=messages,
            extra={"temperature": 0.1, "stream": False},
        )
    except Exception as exc:
        if not _should_try_chat_endpoint(exc):
            raise
        response, _ = client.chat(
            model=os.getenv("QWEN_MODEL", "qwen3.7-max"),
            messages=messages,
            extra={"temperature": 0.1, "stream": False},
        )
    return _extract_llm_text(response)


def _fallback_merged_answer(rag_answer: str, ifc_context: str) -> str:
    parts: list[str] = []
    if rag_answer:
        parts.append(rag_answer.strip())
    if ifc_context:
        parts.append("\nКонтекст из IFC-модели:\n" + ifc_context.strip())
    return "\n\n".join(parts).strip() or "Не удалось найти релевантные данные."


async def _ask_rag_and_ifc(
    *,
    organization_id: int,
    project_id: int,
    question: str,
    top_k: int,
) -> dict[str, Any]:
    rag_task = asyncio.create_task(
        RagClient().ask(
            project_id=project_id,
            organization_id=organization_id,
            question=question,
            top_k=top_k,
        )
    )
    ifc_task = asyncio.create_task(
        IfcClient().get_context(
            project_id=project_id,
            organization_id=organization_id,
            question=question,
            max_models=3,
            limit=15,
        )
    )

    rag_result: dict[str, Any] = {}
    ifc_result: dict[str, Any] = {}
    rag_error: str | None = None
    ifc_error: str | None = None

    try:
        rag_result = await rag_task
    except Exception as exc:
        rag_error = f"{exc.__class__.__name__}: {exc}"
        logger.warning("rag_ask_failed project_id=%s: %s", project_id, rag_error)

    try:
        ifc_result = await ifc_task
    except Exception as exc:
        ifc_error = f"{exc.__class__.__name__}: {exc}"
        logger.warning("ifc_context_failed project_id=%s: %s", project_id, ifc_error)

    if rag_error and ifc_error:
        raise HTTPException(
            status_code=502,
            detail=f"RAG и IFC недоступны: RAG={rag_error}; IFC={ifc_error}",
        )

    rag_answer = str(rag_result.get("answer") or "")
    ifc_context = str(ifc_result.get("context_text") or "")
    if ifc_context:
        try:
            answer = await asyncio.to_thread(
                _call_llm_for_merged_answer,
                question,
                rag_answer,
                ifc_context,
            )
        except Exception as exc:
            logger.warning("merged_llm_failed project_id=%s: %s", project_id, exc)
            answer = _fallback_merged_answer(rag_answer, ifc_context)
    else:
        answer = rag_answer or _fallback_merged_answer("", "")

    retrieval_mode = "documents+ifc" if ifc_context else "documents"
    if rag_error:
        retrieval_mode = "ifc_only"
    if ifc_error and not rag_error:
        retrieval_mode = "documents"

    return {
        "answer": answer,
        "selected_chunk_ids": rag_result.get("selected_chunk_ids") or [],
        "selected_asset_ids": rag_result.get("selected_asset_ids") or [],
        "pages": rag_result.get("pages") or [],
        "sources": rag_result.get("sources") or [],
        "assets": rag_result.get("assets") or [],
        "ifc_sources": ifc_result.get("items") or [],
        "retrieval_mode": retrieval_mode,
        "errors": {"rag": rag_error, "ifc": ifc_error},
    }


def _build_chat_response_payload(result: dict[str, Any]) -> dict[str, Any]:
    sources = [SourceRef(**row) for row in result.get("sources") or []]
    pages = [PageViewerRef(**row) for row in result.get("pages") or []]
    assets = [SourceAssetRef(**row) for row in result.get("assets") or []]
    ifc_sources = [IfcSourceRef(**row) for row in result.get("ifc_sources") or []]
    return {
        "answer": str(result.get("answer") or ""),
        "selected_chunk_ids": [int(x) for x in result.get("selected_chunk_ids") or []],
        "selected_asset_ids": [int(x) for x in result.get("selected_asset_ids") or []],
        "pages": [p.model_dump() for p in pages],
        "sources": [s.model_dump() for s in sources],
        "assets": [a.model_dump() for a in assets],
        "ifc_sources": [s.model_dump() for s in ifc_sources],
        "retrieval_mode": result.get("retrieval_mode") or "documents+ifc",
        "errors": result.get("errors") or {},
    }


def _history_references(response_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "selected_chunk_ids": response_payload.get("selected_chunk_ids") or [],
        "selected_asset_ids": response_payload.get("selected_asset_ids") or [],
        "pages": response_payload.get("pages") or [],
        "sources": response_payload.get("sources") or [],
        "assets": response_payload.get("assets") or [],
        "ifc_sources": response_payload.get("ifc_sources") or [],
        "retrieval_mode": response_payload.get("retrieval_mode") or "documents+ifc",
        "errors": response_payload.get("errors") or {},
    }


def _save_history_safely(
    db: Session,
    *,
    project_id: int,
    user_id: int,
    question: str,
    answer: str,
    user_label: str | None,
    references: dict[str, Any],
) -> None:
    try:
        qa = QASession(
            project_id=project_id,
            user_id=user_id,
            question=question,
            answer=answer,
            user_label=user_label,
            references=json.dumps(references, ensure_ascii=False),
        )
        db.add(qa)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("chat_history_save_failed project_id=%s", project_id)


async def _run_chat_job(
    job_id: str,
    *,
    organization_id: int,
    project_id: int,
    user_id: int,
    question: str,
    user_label: str | None,
    top_k: int,
) -> None:
    async with CHAT_JOBS_LOCK:
        if job_id in CHAT_JOBS:
            CHAT_JOBS[job_id].update(
                {"status": "running", "stage": "Поиск в RAG и IFC", "updated_at": _now_iso()}
            )

    db = SessionLocal()
    try:
        get_project_for_org(db, project_id, organization_id)
        result = await _ask_rag_and_ifc(
            organization_id=organization_id,
            project_id=project_id,
            question=question.strip(),
            top_k=top_k,
        )
        response_payload = _build_chat_response_payload(result)
        _save_history_safely(
            db,
            project_id=project_id,
            user_id=user_id,
            question=question,
            answer=response_payload["answer"],
            user_label=user_label,
            references=_history_references(response_payload),
        )
        async with CHAT_JOBS_LOCK:
            if job_id in CHAT_JOBS:
                CHAT_JOBS[job_id].update(
                    {
                        "status": "done",
                        "stage": "Ответ готов",
                        "result": response_payload,
                        "updated_at": _now_iso(),
                    }
                )
    except Exception as exc:
        logger.exception("chat_job_failed job_id=%s project_id=%s", job_id, project_id)
        error_text = re.sub(r"\s+", " ", f"{exc.__class__.__name__}: {exc}").strip()
        async with CHAT_JOBS_LOCK:
            if job_id in CHAT_JOBS:
                CHAT_JOBS[job_id].update(
                    {
                        "status": "error",
                        "stage": "Ошибка анализа",
                        "error": error_text,
                        "updated_at": _now_iso(),
                    }
                )
    finally:
        db.close()


@router.post("/chat/jobs")
async def start_chat_job(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is empty")

    get_project_for_org(db, payload.project_id, user.organization_id)
    await _cleanup_chat_jobs()

    job_id = str(uuid.uuid4())
    async with CHAT_JOBS_LOCK:
        CHAT_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "stage": "Задача поставлена в очередь",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "created_ts": datetime.now(timezone.utc).timestamp(),
            "project_id": int(payload.project_id),
            "question": question,
        }

    asyncio.create_task(
        _run_chat_job(
            job_id,
            organization_id=int(user.organization_id),
            project_id=int(payload.project_id),
            user_id=int(user.id),
            question=question,
            user_label=payload.user_label,
            top_k=int(payload.top_k),
        )
    )
    logger.info("chat_job_started job_id=%s project_id=%s", job_id, payload.project_id)
    return {"job_id": job_id, "status": "queued", "stage": "Задача поставлена в очередь"}


@router.get("/chat/jobs/{job_id}")
async def get_chat_job(job_id: str, user: User = Depends(get_current_user)):
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")
    async with CHAT_JOBS_LOCK:
        job = CHAT_JOBS.get(job_id)
        if not job:
            raise HTTPException(
                status_code=404,
                detail="Chat job not found. Возможно, backend был перезапущен.",
            )
        return {k: v for k, v in job.items() if k != "created_ts"}


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")

    try:
        get_project_for_org(db, payload.project_id, user.organization_id)
        result = await _ask_rag_and_ifc(
            organization_id=int(user.organization_id),
            project_id=int(payload.project_id),
            question=payload.question.strip(),
            top_k=int(payload.top_k),
        )
        response_payload = _build_chat_response_payload(result)
        _save_history_safely(
            db,
            project_id=payload.project_id,
            user_id=user.id,
            question=payload.question,
            answer=response_payload["answer"],
            user_label=payload.user_label,
            references=_history_references(response_payload),
        )
        return ChatResponse(
            answer=response_payload["answer"],
            selected_chunk_ids=response_payload["selected_chunk_ids"],
            selected_asset_ids=response_payload["selected_asset_ids"],
            pages=[PageViewerRef(**row) for row in response_payload["pages"]],
            sources=[SourceRef(**row) for row in response_payload["sources"]],
            assets=[SourceAssetRef(**row) for row in response_payload["assets"]],
            ifc_sources=[IfcSourceRef(**row) for row in response_payload["ifc_sources"]],
            retrieval_mode=response_payload["retrieval_mode"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("chat_unexpected_error project_id=%s", payload.project_id)
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка API chat: {exc.__class__.__name__}: {exc}",
        ) from exc
