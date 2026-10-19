from __future__ import annotations

import os
import re
import sqlite3

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from ..config import settings
from ..security import generate_api_key, generate_password, hash_password


class Base(DeclarativeBase):
    pass


_IS_SQLITE = settings.SQLALCHEMY_DATABASE_URL.startswith("sqlite")
# The API process and the worker share one SQLite file: a login or an inspector decision (short write) must wait for
# the worker's write transaction instead of failing with "database is locked" after the 5 s default.
SQLITE_BUSY_TIMEOUT_MS = max(30000, int(os.getenv("API_SQLITE_BUSY_TIMEOUT_MS", "60000") or 60000))

engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000.0} if _IS_SQLITE else {},
)


def apply_sqlite_pragmas(dbapi_connection, _record=None) -> None:
    """WAL (readers and the single writer no longer block each other), a long busy timeout and synchronous=NORMAL
    (safe with WAL). WAL stays optional: on a filesystem that cannot map its shared memory the journal mode simply
    remains as it was, and API_SQLITE_WAL=0 switches it off."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        if os.getenv("API_SQLITE_WAL", "1").strip().lower() not in ("0", "false", "no", "off"):
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
    finally:
        cursor.close()


if _IS_SQLITE:
    event.listen(engine, "connect", apply_sqlite_pragmas)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def release_write_lock(db) -> None:
    """Commit the writes made so far so SQLite's single write lock is free again before a long CPU/OCR stage.

    Only for SQLite (other dialects keep one transaction). The loaded objects are kept as they are
    (expire_on_commit off for this commit): everything flushed is already what the session holds, so a stage
    boundary does not trigger a reload of every document/fragment row. A retry re-runs the stages from the start
    and every write is an upsert keyed by (process, parameter, group), so a partially committed run is harmless."""
    if db.get_bind().dialect.name != "sqlite":
        return
    previous = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = previous


def _sqlite_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return column in cols


def _sqlite_ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    if _sqlite_has_column(conn, table, column):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "org"


def _run_sqlite_migrations() -> None:
    if not settings.SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
        return

    path = str(settings.SQLITE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0)
    try:
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='organizations'").fetchone():
            _sqlite_ensure_column(conn, "organizations", "slug", "TEXT")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
            _sqlite_ensure_column(conn, "users", "login", "TEXT")
            _sqlite_ensure_column(conn, "users", "password_hash", "TEXT")
            _sqlite_ensure_column(conn, "users", "email", "TEXT")
            _sqlite_ensure_column(conn, "users", "api_key", "TEXT")
            _sqlite_ensure_column(conn, "users", "role", "TEXT DEFAULT 'INSPECTOR'")
            conn.execute("UPDATE users SET role = 'INSPECTOR' WHERE role IS NULL")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_documents'").fetchone():
            _sqlite_ensure_column(conn, "project_documents", "meta_path", "TEXT")
            _sqlite_ensure_column(conn, "project_documents", "pages_count", "INTEGER DEFAULT 0")
            _sqlite_ensure_column(conn, "project_documents", "first_page_text", "TEXT")
            _sqlite_ensure_column(conn, "project_documents", "status", "TEXT DEFAULT 'ready'")
            _sqlite_ensure_column(conn, "project_documents", "updated_at", "DATETIME")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='source_fragments'").fetchone():
            _sqlite_ensure_column(conn, "source_fragments", "source_system", "TEXT DEFAULT 'manual'")
            _sqlite_ensure_column(conn, "source_fragments", "external_id", "TEXT")
            _sqlite_ensure_column(conn, "source_fragments", "metadata_json", "JSON")
            _sqlite_ensure_column(conn, "source_fragments", "bbox_pdf", "JSON")
            _sqlite_ensure_column(conn, "source_fragments", "page_width", "REAL")
            _sqlite_ensure_column(conn, "source_fragments", "page_height", "REAL")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='document_versions'").fetchone():
            _sqlite_ensure_column(conn, "document_versions", "object_id", "TEXT")
            _sqlite_ensure_column(conn, "document_versions", "dataset_file_id", "TEXT")
            _sqlite_ensure_column(conn, "document_versions", "dataset_split", "TEXT")
            _sqlite_ensure_column(conn, "document_versions", "dataset_stage", "TEXT")
            _sqlite_ensure_column(conn, "document_versions", "dataset_section", "TEXT")
            _sqlite_ensure_column(conn, "document_versions", "dataset_metadata", "JSON")
            _sqlite_ensure_column(conn, "document_versions", "doc_stage", "TEXT")
            _sqlite_ensure_column(conn, "document_versions", "document_code", "TEXT")
            _sqlite_ensure_column(conn, "document_versions", "approval_status", "TEXT DEFAULT 'UNKNOWN'")
            _sqlite_ensure_column(conn, "document_versions", "approval_date", "DATETIME")
            _sqlite_ensure_column(conn, "document_versions", "file_hash", "TEXT")
            _sqlite_ensure_column(conn, "document_versions", "file_path", "TEXT")
            _sqlite_ensure_column(conn, "document_versions", "predecessor_id", "INTEGER")
            _sqlite_ensure_column(conn, "document_versions", "successor_id", "INTEGER")
            _sqlite_ensure_column(conn, "document_versions", "uploaded_at", "DATETIME")
            conn.execute(
                """
                UPDATE document_versions
                SET doc_stage = COALESCE(doc_stage, document_stage),
                    file_hash = COALESCE(file_hash, content_hash),
                    approval_status = COALESCE(approval_status, 'UNKNOWN'),
                    uploaded_at = COALESCE(uploaded_at, created_at, CURRENT_TIMESTAMP)
                """
            )
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='upload_jobs'").fetchone():
            _sqlite_ensure_column(conn, "upload_jobs", "organization_id", "INTEGER")
            _sqlite_ensure_column(conn, "upload_jobs", "user_id", "INTEGER")
            _sqlite_ensure_column(conn, "upload_jobs", "temp_path", "TEXT")
            _sqlite_ensure_column(conn, "upload_jobs", "file_size", "INTEGER")
            _sqlite_ensure_column(conn, "upload_jobs", "process_id", "TEXT")
            _sqlite_ensure_column(conn, "upload_jobs", "source_type", "TEXT DEFAULT 'rag'")
            _sqlite_ensure_column(conn, "upload_jobs", "status", "TEXT DEFAULT 'queued'")
            _sqlite_ensure_column(conn, "upload_jobs", "stage", "TEXT")
            _sqlite_ensure_column(conn, "upload_jobs", "progress", "INTEGER DEFAULT 0")
            _sqlite_ensure_column(conn, "upload_jobs", "detail", "TEXT")
            _sqlite_ensure_column(conn, "upload_jobs", "document_id", "INTEGER")
            _sqlite_ensure_column(conn, "upload_jobs", "updated_at", "DATETIME")
            _sqlite_ensure_column(conn, "upload_jobs", "finished_at", "DATETIME")
            conn.execute(
                """
                UPDATE upload_jobs
                SET status = 'failed',
                    stage = 'Обработка прервана',
                    progress = 100,
                    detail = COALESCE(detail, 'API-сервис был перезапущен во время обработки файла'),
                    finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
                WHERE status = 'processing'
                """
            )
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='evidence_fragments'").fetchone():
            _sqlite_ensure_column(conn, "evidence_fragments", "dataset_file_id", "TEXT")
            _sqlite_ensure_column(conn, "evidence_fragments", "bbox_pdf", "JSON")
            _sqlite_ensure_column(conn, "evidence_fragments", "page_width", "REAL")
            _sqlite_ensure_column(conn, "evidence_fragments", "page_height", "REAL")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='params'").fetchone():
            _sqlite_ensure_column(conn, "params", "matrix_code", "TEXT")
            _sqlite_ensure_column(conn, "params", "scoring_code", "TEXT")
            _sqlite_ensure_column(conn, "params", "aliases_json", "JSON")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='inspection_processes'").fetchone():
            _sqlite_ensure_column(conn, "inspection_processes", "finalized_by_user_id", "INTEGER")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='protocols'").fetchone():
            _sqlite_ensure_column(conn, "protocols", "finalized_by_user_id", "INTEGER")
            _sqlite_ensure_column(conn, "protocols", "unfinalized_at", "DATETIME")
            _sqlite_ensure_column(conn, "protocols", "unfinalized_by_user_id", "INTEGER")
            _sqlite_ensure_column(conn, "protocols", "unfinalize_reason", "TEXT")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='evidence_groups'").fetchone():
            _sqlite_ensure_column(conn, "evidence_groups", "group_key", "TEXT")
            _sqlite_ensure_column(conn, "evidence_groups", "evidence_basis_hash", "TEXT")
            _sqlite_ensure_column(conn, "evidence_groups", "needs_reverification", "BOOLEAN DEFAULT 0")
            _sqlite_ensure_column(conn, "evidence_groups", "basis_changed_at", "DATETIME")
            conn.execute("UPDATE evidence_groups SET needs_reverification = 0 WHERE needs_reverification IS NULL")
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dispute_logs'").fetchone():
            _sqlite_ensure_column(conn, "dispute_logs", "reason_code", "TEXT NOT NULL DEFAULT 'OTHER'")
            _sqlite_ensure_column(conn, "dispute_logs", "ai_verdict", "TEXT NOT NULL DEFAULT 'PENDING_REVIEW'")
            _sqlite_ensure_column(conn, "dispute_logs", "suggested_fix", "TEXT")
        conn.commit()
    finally:
        conn.close()


def _run_postgres_migrations() -> None:
    if not settings.SQLALCHEMY_DATABASE_URL.startswith("postgres"):
        return

    statements = [
        "ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS dataset_file_id VARCHAR(128)",
        "ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS dataset_split VARCHAR(64)",
        "ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS dataset_stage VARCHAR(64)",
        "ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS dataset_section VARCHAR(128)",
        "ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS dataset_metadata JSON",
        "ALTER TABLE source_fragments ADD COLUMN IF NOT EXISTS bbox_pdf JSON",
        "ALTER TABLE source_fragments ADD COLUMN IF NOT EXISTS page_width DOUBLE PRECISION",
        "ALTER TABLE source_fragments ADD COLUMN IF NOT EXISTS page_height DOUBLE PRECISION",
        "ALTER TABLE evidence_fragments ADD COLUMN IF NOT EXISTS dataset_file_id VARCHAR(128)",
        "ALTER TABLE evidence_fragments ADD COLUMN IF NOT EXISTS bbox_pdf JSON",
        "ALTER TABLE evidence_fragments ADD COLUMN IF NOT EXISTS page_width DOUBLE PRECISION",
        "ALTER TABLE evidence_fragments ADD COLUMN IF NOT EXISTS page_height DOUBLE PRECISION",
        "ALTER TABLE params ADD COLUMN IF NOT EXISTS matrix_code VARCHAR(64)",
        "ALTER TABLE params ADD COLUMN IF NOT EXISTS scoring_code VARCHAR(64)",
        "ALTER TABLE params ADD COLUMN IF NOT EXISTS aliases_json JSON",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(32) DEFAULT 'INSPECTOR'",
        "ALTER TABLE inspection_processes ADD COLUMN IF NOT EXISTS finalized_by_user_id INTEGER",
        "ALTER TABLE protocols ADD COLUMN IF NOT EXISTS finalized_by_user_id INTEGER",
        "ALTER TABLE protocols ADD COLUMN IF NOT EXISTS unfinalized_at TIMESTAMP",
        "ALTER TABLE protocols ADD COLUMN IF NOT EXISTS unfinalized_by_user_id INTEGER",
        "ALTER TABLE protocols ADD COLUMN IF NOT EXISTS unfinalize_reason TEXT",
        "ALTER TABLE evidence_groups ADD COLUMN IF NOT EXISTS group_key VARCHAR(255)",
        "ALTER TABLE evidence_groups ADD COLUMN IF NOT EXISTS evidence_basis_hash VARCHAR(128)",
        "ALTER TABLE evidence_groups ADD COLUMN IF NOT EXISTS needs_reverification BOOLEAN DEFAULT FALSE",
        "ALTER TABLE evidence_groups ADD COLUMN IF NOT EXISTS basis_changed_at TIMESTAMP",
        "ALTER TABLE upload_jobs ADD COLUMN IF NOT EXISTS file_size INTEGER",
        "ALTER TABLE upload_jobs ADD COLUMN IF NOT EXISTS process_id VARCHAR(64)",
        "ALTER TABLE dispute_logs ADD COLUMN IF NOT EXISTS reason_code VARCHAR(64) NOT NULL DEFAULT 'OTHER'",
        "ALTER TABLE dispute_logs ADD COLUMN IF NOT EXISTS ai_verdict VARCHAR(32) NOT NULL DEFAULT 'PENDING_REVIEW'",
        "ALTER TABLE dispute_logs ADD COLUMN IF NOT EXISTS suggested_fix TEXT",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _backfill_auth_fields() -> None:
    from .models import Organization, User

    db = SessionLocal()
    try:
        orgs = db.execute(select(Organization)).scalars().all()
        seen_slugs: set[str] = set()
        for org in orgs:
            if org.slug:
                seen_slugs.add(org.slug)
                continue
            base = _slugify(org.name)
            slug = base
            n = 2
            while slug in seen_slugs:
                slug = f"{base}-{n}"
                n += 1
            org.slug = slug
            seen_slugs.add(slug)

        users = db.execute(select(User)).scalars().all()
        seen_logins: set[str] = set(u.login for u in users if getattr(u, "login", None))
        for user in users:
            changed = False
            if not user.login:
                base = _slugify(user.email or f"user-{user.id}")
                login = base
                n = 2
                while login in seen_logins:
                    login = f"{base}-{n}"
                    n += 1
                user.login = login
                seen_logins.add(login)
                changed = True
            if not user.password_hash:
                pwd = generate_password()
                user.password_hash = hash_password(pwd)
                changed = True
            if not user.api_key:
                user.api_key = generate_api_key()
                changed = True
            if changed:
                db.add(user)
        db.commit()
    finally:
        db.close()


def _bootstrap_default_admin() -> None:
    from .models import Organization, User

    db = SessionLocal()
    try:
        any_user = db.execute(select(User.id).limit(1)).first()
        if any_user:
            return

        org_name = os.getenv("DEFAULT_ADMIN_ORG", "Default")
        org_slug = _slugify(org_name)
        login = os.getenv("DEFAULT_ADMIN_LOGIN", "admin")
        email = os.getenv("DEFAULT_ADMIN_EMAIL", "admin@local")
        password = os.getenv("DEFAULT_ADMIN_PASSWORD", generate_password())
        api_key = os.getenv("DEFAULT_ADMIN_API_KEY", generate_api_key())

        org = Organization(name=org_name, slug=org_slug)
        db.add(org)
        db.flush()

        admin = User(
            login=login,
            email=email,
            password_hash=hash_password(password),
            api_key=api_key,
            is_admin=True,
            role="ADMIN",
            organization_id=org.id,
        )
        db.add(admin)
        db.commit()

        creds_path = settings.DATA_DIR / "initial_admin_credentials.txt"
        creds_path.write_text(
            "Initial admin account\n"
            f"login: {login}\n"
            f"password: {password}\n"
            f"api_key: {api_key}\n"
            f"organization: {org_name}\n",
            encoding="utf-8",
        )
        print(f"[BOOTSTRAP] Created default admin user: {login} | password: {password} | org: {org_name}")
    finally:
        db.close()


def _seed_logical_rules() -> None:
    """Keep the `logical_rules` table (ТЗ-10 `Logical_Rules`) in sync with the
    hand-authored `LOGICAL_RULES` tuple that actually runs. Descriptive
    columns are refreshed on every startup; `is_active` is seeded once and
    then left alone, since that is the one column an admin is meant to edit
    at runtime (see `LogicalRuleRecord` docstring) and a restart must not
    silently undo that choice."""
    from .models import LogicalRuleRecord
    from ..domain.logical_analysis import LOGICAL_RULES

    db = SessionLocal()
    try:
        existing = {row.rule_id: row for row in db.execute(select(LogicalRuleRecord)).scalars().all()}
        for rule in LOGICAL_RULES:
            row = existing.get(rule.rule_id)
            if row is None:
                db.add(LogicalRuleRecord(
                    rule_id=rule.rule_id,
                    rule_name=rule.rule_name,
                    condition=rule.condition_description,
                    expected=rule.implied_description,
                    normative_base=rule.normative_base,
                    criticality=rule.criticality,
                    confidence=rule.confidence,
                    is_active=rule.is_active,
                ))
            else:
                row.rule_name = rule.rule_name
                row.condition = rule.condition_description
                row.expected = rule.implied_description
                row.normative_base = rule.normative_base
                row.criticality = rule.criticality
                row.confidence = rule.confidence
                db.add(row)
        from ..domain.free_hypotheses.logic_rules import rule_descriptors

        for item in rule_descriptors():
            row = existing.get(item["rule_id"])
            if row is None:
                db.add(LogicalRuleRecord(**item))
            else:
                for key in ("rule_name", "condition", "expected", "normative_base", "criticality", "confidence"):
                    setattr(row, key, item[key])
                db.add(row)
        db.commit()
    finally:
        db.close()


def _record_schema_migration(version: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(255) PRIMARY KEY,
                    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO schema_migrations (version)
                VALUES (:version)
                ON CONFLICT(version) DO NOTHING
                """
            ),
            {"version": version},
        )


def init_db() -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _record_schema_migration("20260914_case10_domain")
    _record_schema_migration("20260915_case10_v3_core")
    _record_schema_migration("20260919_case10_verification_lifecycle")
    _record_schema_migration("20260919_case10_incremental_recompute")
    _record_schema_migration("20260921_case10_tz10_tables")
    _run_sqlite_migrations()
    _run_postgres_migrations()
    _backfill_auth_fields()
    _seed_logical_rules()
    _bootstrap_default_admin()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
