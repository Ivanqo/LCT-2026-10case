from __future__ import annotations

import os
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from ..config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False} if settings.SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _sqlite_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return column in [r[1] for r in cur.fetchall()]


def _sqlite_ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    if not _sqlite_has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def _run_sqlite_migrations() -> None:
    if not settings.SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
        return
    path = str(settings.SQLITE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for table in ("documents", "pages", "regions", "chunks"):
            if conn.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'").fetchone():
                _sqlite_ensure_column(conn, table, "organization_id", "INTEGER")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='documents'").fetchone():
            _sqlite_ensure_column(conn, "documents", "status", "TEXT DEFAULT 'processing'")
            _sqlite_ensure_column(conn, "documents", "stage", "TEXT")
            _sqlite_ensure_column(conn, "documents", "progress", "INTEGER DEFAULT 0")
            _sqlite_ensure_column(conn, "documents", "detail", "TEXT")
            _sqlite_ensure_column(conn, "documents", "processing_started_at", "DATETIME")
            _sqlite_ensure_column(conn, "documents", "processing_finished_at", "DATETIME")
            conn.execute("UPDATE documents SET status = COALESCE(status, 'ready')")
            conn.execute("UPDATE documents SET progress = COALESCE(progress, 100)")
            conn.execute("UPDATE documents SET stage = COALESCE(stage, 'Документ готов')")
            conn.execute(
                """
                UPDATE documents
                SET status = 'failed',
                    stage = 'Обработка прервана',
                    detail = COALESCE(detail, 'Сервис RAG был перезапущен во время обработки документа'),
                    processing_finished_at = COALESCE(processing_finished_at, CURRENT_TIMESTAMP)
                WHERE status = 'processing'
                """
            )
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _run_sqlite_migrations()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
