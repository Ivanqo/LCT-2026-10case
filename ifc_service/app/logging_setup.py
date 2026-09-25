from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import settings


def setup_logging(service_name: str = "ifc") -> None:
    log_path = Path(settings.LOG_DIR) / f"{service_name}.log"
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if root.handlers:
        return

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    fh = RotatingFileHandler(log_path, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)

    root.addHandler(ch)
    root.addHandler(fh)
