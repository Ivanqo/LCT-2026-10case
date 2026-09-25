from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from ..config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _ensure_postgres_columns() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS organization_id INTEGER"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS project_id INTEGER"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS original_filename VARCHAR(512)"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS stored_path VARCHAR(2048)"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS file_hash VARCHAR(64)"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS schema VARCHAR(64)"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS units VARCHAR(128)"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS status VARCHAR(32) DEFAULT 'queued'"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS error TEXT"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now()"))
        conn.execute(text("ALTER TABLE IF EXISTS models ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE IF EXISTS elements ADD COLUMN IF NOT EXISTS storey_guid VARCHAR(64)"))
        conn.execute(text("ALTER TABLE IF EXISTS elements ADD COLUMN IF NOT EXISTS container_guid VARCHAR(64)"))
        conn.execute(text("ALTER TABLE IF EXISTS properties ADD COLUMN IF NOT EXISTS pset VARCHAR(256)"))
        conn.execute(text("ALTER TABLE IF EXISTS properties ADD COLUMN IF NOT EXISTS value_text TEXT"))
        conn.execute(text("ALTER TABLE IF EXISTS properties ADD COLUMN IF NOT EXISTS value_num DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE IF EXISTS properties ADD COLUMN IF NOT EXISTS value_bool VARCHAR(8)"))
        conn.execute(text("ALTER TABLE IF EXISTS quantities ADD COLUMN IF NOT EXISTS qto VARCHAR(256)"))
        conn.execute(text("ALTER TABLE IF EXISTS quantities ADD COLUMN IF NOT EXISTS value_num DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE IF EXISTS quantities ADD COLUMN IF NOT EXISTS unit VARCHAR(64)"))
        conn.execute(text("ALTER TABLE IF EXISTS metrics ADD COLUMN IF NOT EXISTS area DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE IF EXISTS metrics ADD COLUMN IF NOT EXISTS volume DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE IF EXISTS metrics ADD COLUMN IF NOT EXISTS bbox_min JSONB"))
        conn.execute(text("ALTER TABLE IF EXISTS metrics ADD COLUMN IF NOT EXISTS bbox_max JSONB"))
        conn.execute(text("ALTER TABLE IF EXISTS metrics ADD COLUMN IF NOT EXISTS source VARCHAR(32)"))


def init_db() -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _ensure_postgres_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
