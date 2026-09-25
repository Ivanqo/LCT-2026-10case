from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import settings
from .request_context import get_request_id, get_user_id


class JsonLogFormatter(logging.Formatter):
    """ТЗ 13: structured JSON logs. Every record carries timestamp (ISO8601
    UTC), level, service, message, request_id and user_id (the latter two are
    null when there is no request in context, e.g. a background job)."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service,
            "message": record.getMessage(),
            "request_id": get_request_id(),
            "user_id": get_user_id(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(service_name: str = "api") -> None:
    """Configure per-service logging (console + rotating file)."""
    log_path = Path(settings.LOG_DIR) / f"{service_name}.log"
    formatter = JsonLogFormatter(service_name)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Avoid duplicate handlers when reloading
    if root.handlers:
        return

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    fh = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)

    root.addHandler(ch)
    root.addHandler(fh)
