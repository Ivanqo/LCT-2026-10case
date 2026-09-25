from __future__ import annotations

import json
import logging
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)


PROCESS_EVENT_QUEUE_NAME = "case10.process.events"
PROCESS_JOB_QUEUE_NAME = "case10.process.jobs"
PROCESS_JOB_ERROR_QUEUE_NAME = "case10.process.errors"
PROCESS_JOB_DLX = "case10.process.dlx"

# Backward-compatible alias used by older diagnostics/checkpoints.
QUEUE_NAME = PROCESS_EVENT_QUEUE_NAME


def _json_log(level: int, event: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


def declare_process_job_topology(channel: Any) -> None:
    channel.exchange_declare(exchange=PROCESS_JOB_DLX, exchange_type="direct", durable=True)
    channel.queue_declare(queue=PROCESS_JOB_ERROR_QUEUE_NAME, durable=True)
    channel.queue_bind(
        queue=PROCESS_JOB_ERROR_QUEUE_NAME,
        exchange=PROCESS_JOB_DLX,
        routing_key=PROCESS_JOB_ERROR_QUEUE_NAME,
    )
    channel.queue_declare(
        queue=PROCESS_JOB_QUEUE_NAME,
        durable=True,
        arguments={
            "x-dead-letter-exchange": PROCESS_JOB_DLX,
            "x-dead-letter-routing-key": PROCESS_JOB_ERROR_QUEUE_NAME,
        },
    )


def _publish(queue: str, payload: dict[str, Any], *, headers: dict[str, Any] | None = None, declare_jobs: bool = False) -> bool:
    if not settings.RABBITMQ_URL:
        return False
    try:
        import pika  # type: ignore
    except Exception:
        logger.debug("RabbitMQ publisher is disabled because pika is not installed")
        return False

    connection = None
    try:
        connection = pika.BlockingConnection(pika.URLParameters(settings.RABBITMQ_URL))
        channel = connection.channel()
        if declare_jobs:
            declare_process_job_topology(channel)
        else:
            channel.queue_declare(queue=queue, durable=True)
        channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
                headers=headers or {},
            ),
        )
        _json_log(
            logging.INFO,
            "rabbitmq_publish",
            queue=queue,
            process_id=payload.get("process_id"),
            job_id=payload.get("job_id"),
            attempt=payload.get("attempt"),
            status=payload.get("status"),
        )
        return True
    except Exception as exc:
        _json_log(
            logging.WARNING,
            "rabbitmq_publish_failed",
            queue=queue,
            process_id=payload.get("process_id"),
            job_id=payload.get("job_id"),
            attempt=payload.get("attempt"),
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def publish_process_event(payload: dict[str, Any]) -> None:
    _publish(PROCESS_EVENT_QUEUE_NAME, payload)


def publish_process_job(payload: dict[str, Any], *, headers: dict[str, Any] | None = None) -> bool:
    return _publish(PROCESS_JOB_QUEUE_NAME, payload, headers=headers, declare_jobs=True)


def publish_process_error(payload: dict[str, Any], *, headers: dict[str, Any] | None = None) -> bool:
    return _publish(PROCESS_JOB_ERROR_QUEUE_NAME, payload, headers=headers, declare_jobs=True)


def redis_get_json(key: str) -> dict[str, Any] | None:
    if not settings.REDIS_URL:
        return None
    try:
        import redis  # type: ignore
    except Exception:
        return None
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5)
        raw = client.get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def redis_set_json(key: str, value: dict[str, Any], ttl_seconds: int = 24 * 60 * 60) -> None:
    if not settings.REDIS_URL:
        return
    try:
        import redis  # type: ignore
    except Exception:
        return
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5)
        client.setex(key, ttl_seconds, json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return
