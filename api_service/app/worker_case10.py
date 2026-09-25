from __future__ import annotations

import json
import logging
import time
from typing import Any

from .config import settings
from .db.session import SessionLocal, init_db
from .domain.gpu import detect_gpu
from .domain.v3_jobs import execute_process_job, publish_error_for_result, publish_job_message, recover_queued_jobs
from .domain.v3_messaging import PROCESS_JOB_QUEUE_NAME, declare_process_job_topology
from .logging_setup import setup_logging


setup_logging("case10_worker")
logger = logging.getLogger("case10_worker")


def main() -> None:
    init_db()
    # This process (not the `api` request/response process) is what actually loads
    # the semantic-anchor SentenceTransformer during extraction (see
    # semantic_similarity.py) -- detect the GPU here explicitly at startup, same as
    # `api`'s FastAPI startup hook does, so a misconfigured grading-server GPU shows
    # up as a loud log line immediately rather than only on the first job processed.
    detect_gpu()
    while True:
        try:
            _run_consumer()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _log(logging.ERROR, "case10_worker_connection_error", error=f"{type(exc).__name__}: {exc}")
            time.sleep(float(settings.CASE10_WORKER_RECONNECT_SECONDS))


def _run_consumer() -> None:
    try:
        import pika  # type: ignore
    except Exception as exc:
        raise RuntimeError("pika is required for CASE10 worker") from exc
    if not settings.RABBITMQ_URL:
        raise RuntimeError("RABBITMQ_URL is required for CASE10 worker")

    connection = pika.BlockingConnection(pika.URLParameters(settings.RABBITMQ_URL))
    channel = connection.channel()
    declare_process_job_topology(channel)
    _recover_jobs()
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=PROCESS_JOB_QUEUE_NAME, on_message_callback=_on_message)
    _log(logging.INFO, "case10_worker_started", queue=PROCESS_JOB_QUEUE_NAME)
    try:
        channel.start_consuming()
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _recover_jobs() -> None:
    db = SessionLocal()
    try:
        recover_queued_jobs(db, publish=True)
    finally:
        db.close()


def _on_message(channel: Any, method: Any, properties: Any, body: bytes) -> None:
    payload: dict[str, Any]
    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("message body must be a JSON object")
    except Exception as exc:
        _log(logging.ERROR, "case10_worker_bad_message", error=f"{type(exc).__name__}: {exc}")
        channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
        return

    db = SessionLocal()
    try:
        result = execute_process_job(db, payload, redelivered=bool(getattr(method, "redelivered", False)))
    finally:
        db.close()

    if result.retry:
        retry_payload = {**payload, "attempt": result.attempt + 1}
        # The DB row is already back to QUEUED. Re-publish a durable retry
        # message, then acknowledge this delivery.
        class _RetryJob:
            id = result.job_id
            process_id = result.process_id
            project_id = payload.get("project_id") or 0
            organization_id = payload.get("organization_id") or 0
            user_id = payload.get("user_id")
            attempt_count = result.attempt
            max_attempts = payload.get("max_attempts") or settings.CASE10_JOB_MAX_ATTEMPTS
            payload_json = retry_payload

        if publish_job_message(_RetryJob()):  # type: ignore[arg-type]
            channel.basic_ack(delivery_tag=method.delivery_tag)
        else:
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        return

    if result.outcome in {"dead_letter", "failed"}:
        publish_error_for_result(result, payload)
    channel.basic_ack(delivery_tag=method.delivery_tag)


def _log(level: int, event: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
