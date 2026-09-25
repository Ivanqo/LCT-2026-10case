from __future__ import annotations

import os
from pathlib import Path
from pydantic import BaseModel


class Settings(BaseModel):
    PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

    DATA_DIR: Path = Path(os.getenv("IFC_DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()
    UPLOADS_DIR: Path = Path(os.getenv("IFC_UPLOADS_DIR", str(DATA_DIR / "ifc"))).resolve()
    LOG_DIR: Path = Path(os.getenv("IFC_LOG_DIR", str(PROJECT_ROOT / "logs"))).resolve()

    # Postgres
    DATABASE_URL: str = os.getenv(
        "IFC_DATABASE_URL",
        "postgresql+psycopg://ifc:ifc@postgres:5432/ifcdb",
    )

    # Celery
    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    CELERY_RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1")


settings = Settings()
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
