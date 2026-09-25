from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import Case10ProcessJob, InspectionProcess
from .v3_messaging import publish_process_error, publish_process_job
from .v3_pipeline import (
    PROCESS_COMPLETED,
    PROCESS_FAILED,
    PROCESS_FINALIZED,
    PROCESS_PARSING,
    PROCESS_PROCESSING,
    PROCESS_QUEUED,
    PROCESS_READY,
    PROCESS_VERIFYING,
    add_audit,
    run_process,
    _set_process_status,
)


logger = logging.getLogger(__name__)

JOB_QUEUED = "QUEUED"
JOB_PROCESSING = "PROCESSING"
JOB_READY = "READY"
JOB_FAILED = "FAILED"
JOB_SKIPPED = "SKIPPED"

JOB_ACTIVE_STATUSES = {JOB_QUEUED, JOB_PROCESSING}
PROCESS_DONE_STATUSES = {PROCESS_READY, PROCESS_VERIFYING, PROCESS_COMPLETED, PROCESS_FINALIZED}


@dataclass(slots=True)
class JobExecutionResult:
    outcome: str
    job_id: str | None
    process_id: str | None
    attempt: int
    retry: bool = False
    error: str | None = None


def enqueue_process_job(
    db: Session,
    process: InspectionProcess,
    *,
    user_id: int | None,
    affected_param_codes: list[str] | None = None,
    reason: str = "api_start",
    max_attempts: int | None = None,
    impact_scope: dict[str, Any] | None = None,
) -> Case10ProcessJob:
    if process.status == PROCESS_FINALIZED:
        raise HTTPException(status_code=409, detail="Finalized protocol cannot be recomputed")

    existing = (
        db.query(Case10ProcessJob)
        .filter(Case10ProcessJob.process_id == str(process.id), Case10ProcessJob.status.in_(JOB_ACTIVE_STATUSES))
        .order_by(Case10ProcessJob.created_at.desc(), Case10ProcessJob.id.desc())
        .first()
    )
    if existing:
        if affected_param_codes is not None:
            merged = sorted({*(str(item) for item in (process.affected_param_codes or [])), *(str(item) for item in affected_param_codes)})
            process.affected_param_codes = merged
            payload = existing.payload_json if isinstance(existing.payload_json, dict) else {}
            payload["affected_param_codes"] = merged
            if impact_scope is not None:
                scopes = payload.get("impact_scopes") if isinstance(payload.get("impact_scopes"), list) else []
                scopes.append(impact_scope)
                payload["impact_scopes"] = scopes
            existing.payload_json = payload
            existing.updated_at = datetime.utcnow()
            db.add(process)
            db.add(existing)
            db.flush()
        _log_job_event(
            logging.INFO,
            "case10_job_reused",
            process_id=process.id,
            job_id=existing.id,
            status=existing.status,
            reason=reason,
        )
        return existing

    now = datetime.utcnow()
    job_id = uuid.uuid4().hex
    if affected_param_codes is not None:
        process.affected_param_codes = affected_param_codes
    payload = {
        "job_id": job_id,
        "process_id": str(process.id),
        "project_id": int(process.project_id),
        "organization_id": int(process.organization_id),
        "object_id": process.object_id,
        "user_id": user_id,
        "affected_param_codes": affected_param_codes if affected_param_codes is not None else (process.affected_param_codes or []),
        "matrix_version": process.matrix_version,
        "reason": reason,
        "enqueued_at": now.isoformat(),
        "impact_scopes": [impact_scope] if impact_scope is not None else [],
    }
    job = Case10ProcessJob(
        id=job_id,
        process_id=str(process.id),
        project_id=int(process.project_id),
        organization_id=int(process.organization_id),
        user_id=user_id,
        status=JOB_QUEUED,
        attempt_count=0,
        max_attempts=int(max_attempts or settings.CASE10_JOB_MAX_ATTEMPTS),
        payload_json=payload,
    )
    db.add(job)
    db.add(process)
    db.flush()
    _set_process_status(
        db,
        process,
        PROCESS_QUEUED,
        user_id=user_id,
        details={"job_id": job_id, "reason": reason, "queued_at": now.isoformat()},
    )
    add_audit(
        db,
        action="JOB_QUEUED",
        user_id=user_id,
        process=process,
        details={
            "job_id": job_id,
            "attempt": 0,
            "max_attempts": int(job.max_attempts or 0),
            "status": JOB_QUEUED,
            "reason": reason,
            "timestamp": now.isoformat(),
        },
    )
    _log_job_event(logging.INFO, "case10_job_queued", **payload, status=JOB_QUEUED, attempt=0)
    return job


def publish_job_message(job: Case10ProcessJob) -> bool:
    payload = _message_payload(job)
    return publish_process_job(
        payload,
        headers={
            "job_id": str(job.id),
            "process_id": str(job.process_id),
            "attempt": payload["attempt"],
        },
    )


def execute_process_job(db: Session, payload: dict[str, Any], *, redelivered: bool = False) -> JobExecutionResult:
    job_id = str(payload.get("job_id") or "")
    process_id = str(payload.get("process_id") or "")
    if not job_id or not process_id:
        error = "Malformed CASE10 job message: job_id and process_id are required"
        _log_job_event(logging.ERROR, "case10_job_malformed", job_id=job_id or None, process_id=process_id or None, error=error)
        return JobExecutionResult("dead_letter", job_id or None, process_id or None, int(payload.get("attempt") or 0), error=error)

    job = db.get(Case10ProcessJob, job_id)
    process = db.get(InspectionProcess, process_id)
    if not job or not process or str(job.process_id) != process_id:
        error = "CASE10 job/process row not found or mismatched"
        _log_job_event(logging.ERROR, "case10_job_missing", job_id=job_id, process_id=process_id, error=error)
        return JobExecutionResult("dead_letter", job_id, process_id, int(payload.get("attempt") or 0), error=error)

    recovered = _recover_completed_job_if_needed(db, job, process)
    if recovered:
        return recovered

    if str(job.status) == JOB_PROCESSING and not redelivered:
        _log_job_event(
            logging.INFO,
            "case10_job_duplicate_in_flight",
            process_id=process_id,
            job_id=job_id,
            attempt=int(job.attempt_count or 0),
            status=job.status,
        )
        return JobExecutionResult("skipped", job_id, process_id, int(job.attempt_count or 0))

    if str(job.status) in {JOB_READY, JOB_SKIPPED, JOB_FAILED}:
        _log_job_event(
            logging.INFO,
            "case10_job_duplicate_terminal",
            process_id=process_id,
            job_id=job_id,
            attempt=int(job.attempt_count or 0),
            status=job.status,
        )
        return JobExecutionResult("skipped", job_id, process_id, int(job.attempt_count or 0))

    attempt = max(int(payload.get("attempt") or 0), int(job.attempt_count or 0) + 1)
    user_id = _optional_int(payload.get("user_id") or job.user_id)
    affected_param_codes = _affected_codes(payload, job)

    _mark_job_processing(db, job, process, user_id=user_id, attempt=attempt, redelivered=redelivered)
    try:
        run_process(db, process_id=str(process.id), user_id=user_id, affected_param_codes=affected_param_codes)
        job = db.get(Case10ProcessJob, job_id)
        process = db.get(InspectionProcess, process_id)
        if not job or not process:
            raise RuntimeError("CASE10 job/process disappeared after successful processing")
        now = datetime.utcnow()
        job.status = JOB_READY
        job.error = None
        job.retry_reason = None
        job.finished_at = now
        job.updated_at = now
        db.add(job)
        add_audit(
            db,
            action="JOB_READY",
            user_id=user_id,
            process=process,
            details={"job_id": job_id, "attempt": attempt, "status": JOB_READY, "timestamp": now.isoformat()},
        )
        db.commit()
        _log_job_event(logging.INFO, "case10_job_ready", process_id=process_id, job_id=job_id, attempt=attempt, status=JOB_READY)
        return JobExecutionResult("ready", job_id, process_id, attempt)
    except Exception as exc:
        db.rollback()
        return _handle_job_exception(db, job_id=job_id, process_id=process_id, user_id=user_id, attempt=attempt, exc=exc)


def recover_queued_jobs(db: Session, *, publish: bool = True) -> list[dict[str, Any]]:
    rows = (
        db.query(Case10ProcessJob)
        .filter(Case10ProcessJob.status.in_((JOB_QUEUED, JOB_PROCESSING)))
        .order_by(Case10ProcessJob.created_at.asc(), Case10ProcessJob.id.asc())
        .all()
    )
    recovered: list[dict[str, Any]] = []
    for job in rows:
        process = db.get(InspectionProcess, str(job.process_id))
        if not process:
            job.status = JOB_FAILED
            job.error = "Inspection process no longer exists"
            job.finished_at = datetime.utcnow()
            db.add(job)
            db.commit()
            recovered.append({"job_id": str(job.id), "status": JOB_FAILED, "published": False})
            continue
        terminal = _recover_completed_job_if_needed(db, job, process)
        if terminal:
            recovered.append({"job_id": str(job.id), "process_id": str(job.process_id), "status": terminal.outcome, "published": False})
            continue
        if str(job.status) == JOB_PROCESSING:
            now = datetime.utcnow()
            job.status = JOB_QUEUED
            job.retry_reason = "worker_recovery"
            job.updated_at = now
            _set_process_status(
                db,
                process,
                PROCESS_QUEUED,
                user_id=_optional_int(job.user_id),
                details={"job_id": str(job.id), "reason": "worker_recovery", "timestamp": now.isoformat()},
            )
            db.add(job)
            db.commit()
        published = publish_job_message(job) if publish else False
        recovered.append({"job_id": str(job.id), "process_id": str(job.process_id), "status": str(job.status), "published": published})
    if recovered:
        _log_job_event(logging.INFO, "case10_job_recovery_scan", recovered=recovered)
    return recovered


def publish_error_for_result(result: JobExecutionResult, payload: dict[str, Any]) -> bool:
    error_payload = {
        **payload,
        "job_id": result.job_id,
        "process_id": result.process_id,
        "attempt": result.attempt,
        "status": JOB_FAILED,
        "error": result.error,
        "failed_at": datetime.utcnow().isoformat(),
    }
    return publish_process_error(error_payload, headers={"job_id": result.job_id or "", "process_id": result.process_id or ""})


def _message_payload(job: Case10ProcessJob) -> dict[str, Any]:
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    return {
        **payload,
        "job_id": str(job.id),
        "process_id": str(job.process_id),
        "project_id": int(job.project_id),
        "organization_id": int(job.organization_id),
        "user_id": _optional_int(job.user_id),
        "attempt": int(job.attempt_count or 0) + 1,
        "max_attempts": int(job.max_attempts or settings.CASE10_JOB_MAX_ATTEMPTS),
    }


def _mark_job_processing(
    db: Session,
    job: Case10ProcessJob,
    process: InspectionProcess,
    *,
    user_id: int | None,
    attempt: int,
    redelivered: bool,
) -> None:
    now = datetime.utcnow()
    job.status = JOB_PROCESSING
    job.attempt_count = int(attempt)
    job.started_at = now
    job.finished_at = None
    job.error = None
    job.retry_reason = None
    job.updated_at = now
    db.add(job)
    _set_process_status(
        db,
        process,
        PROCESS_PROCESSING,
        user_id=user_id,
        details={"job_id": str(job.id), "attempt": attempt, "redelivered": bool(redelivered), "timestamp": now.isoformat()},
    )
    add_audit(
        db,
        action="JOB_STARTED",
        user_id=user_id,
        process=process,
        details={"job_id": str(job.id), "attempt": attempt, "status": JOB_PROCESSING, "timestamp": now.isoformat()},
    )
    db.commit()
    _log_job_event(
        logging.INFO,
        "case10_job_started",
        process_id=process.id,
        job_id=job.id,
        attempt=attempt,
        status=JOB_PROCESSING,
        redelivered=redelivered,
        timestamp=now.isoformat(),
    )


def _handle_job_exception(
    db: Session,
    *,
    job_id: str,
    process_id: str,
    user_id: int | None,
    attempt: int,
    exc: Exception,
) -> JobExecutionResult:
    job = db.get(Case10ProcessJob, job_id)
    process = db.get(InspectionProcess, process_id)
    error = f"{type(exc).__name__}: {exc}"
    retryable = _is_retryable(exc)
    max_attempts = int((job.max_attempts if job else None) or settings.CASE10_JOB_MAX_ATTEMPTS)
    now = datetime.utcnow()
    if job and process and retryable and attempt < max_attempts:
        job.status = JOB_QUEUED
        job.error = error
        job.retry_reason = "retryable_error"
        job.updated_at = now
        _set_process_status(
            db,
            process,
            PROCESS_QUEUED,
            user_id=user_id,
            details={"job_id": job_id, "attempt": attempt, "retry_reason": error, "timestamp": now.isoformat()},
        )
        db.add(job)
        add_audit(
            db,
            action="JOB_RETRY_SCHEDULED",
            user_id=user_id,
            process=process,
            details={
                "job_id": job_id,
                "attempt": attempt,
                "next_attempt": attempt + 1,
                "max_attempts": max_attempts,
                "error": error,
                "timestamp": now.isoformat(),
            },
        )
        db.commit()
        _log_job_event(
            logging.WARNING,
            "case10_job_retry_scheduled",
            process_id=process_id,
            job_id=job_id,
            attempt=attempt,
            next_attempt=attempt + 1,
            max_attempts=max_attempts,
            error=error,
        )
        return JobExecutionResult("retry", job_id, process_id, attempt, retry=True, error=error)

    if job:
        job.status = JOB_FAILED
        job.error = error
        job.retry_reason = "attempts_exhausted" if retryable else "permanent_error"
        job.finished_at = now
        job.updated_at = now
        db.add(job)
    if process:
        process.retry_count = int(attempt)
        process.error = error
        _set_process_status(
            db,
            process,
            PROCESS_FAILED,
            user_id=user_id,
            details={
                "job_id": job_id,
                "attempt": attempt,
                "retryable": retryable,
                "reason": "attempts_exhausted" if retryable else "permanent_error",
                "error": error,
                "timestamp": now.isoformat(),
            },
        )
        add_audit(
            db,
            action="JOB_FAILED",
            user_id=user_id,
            process=process,
            details={
                "job_id": job_id,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "retryable": retryable,
                "error": error,
                "timestamp": now.isoformat(),
            },
        )
    db.commit()
    _log_job_event(
        logging.ERROR,
        "case10_job_failed",
        process_id=process_id,
        job_id=job_id,
        attempt=attempt,
        max_attempts=max_attempts,
        retryable=retryable,
        error=error,
    )
    return JobExecutionResult("failed", job_id, process_id, attempt, retry=False, error=error)


def _recover_completed_job_if_needed(db: Session, job: Case10ProcessJob, process: InspectionProcess) -> JobExecutionResult | None:
    if str(process.status) not in PROCESS_DONE_STATUSES:
        return None
    now = datetime.utcnow()
    if str(job.status) != JOB_READY:
        job.status = JOB_READY if process.status != PROCESS_FINALIZED else JOB_SKIPPED
        job.finished_at = job.finished_at or now
        job.updated_at = now
        db.add(job)
        add_audit(
            db,
            action="JOB_DUPLICATE_SKIPPED",
            user_id=_optional_int(job.user_id),
            process=process,
            details={
                "job_id": str(job.id),
                "attempt": int(job.attempt_count or 0),
                "process_status": process.status,
                "status": job.status,
                "timestamp": now.isoformat(),
            },
        )
        db.commit()
    _log_job_event(
        logging.INFO,
        "case10_job_duplicate_completed",
        process_id=process.id,
        job_id=job.id,
        attempt=int(job.attempt_count or 0),
        process_status=process.status,
        status=job.status,
    )
    return JobExecutionResult("skipped", str(job.id), str(process.id), int(job.attempt_count or 0))


def _affected_codes(payload: dict[str, Any], job: Case10ProcessJob) -> list[str] | None:
    raw = payload.get("affected_param_codes")
    if raw is None and isinstance(job.payload_json, dict):
        raw = job.payload_json.get("affected_param_codes")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    return [str(item) for item in raw if str(item).strip()]


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, HTTPException):
        return int(exc.status_code) in {408, 429, 500, 502, 503, 504}
    return True


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _log_job_event(level: int, event: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))
