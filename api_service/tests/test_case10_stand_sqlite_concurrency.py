"""D1: a login/decision (short write) must not fail with "database is locked" while the worker holds a write
transaction. File-based SQLite, because in-memory databases cannot reproduce cross-connection locking."""
from __future__ import annotations

import threading
import time

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import tempfile
from pathlib import Path

import pytest

from app.db import session as session_module


@pytest.fixture
def tmp_path():
    # own temp dir: pytest's shared base directory can be unreadable for the current user on a developer machine
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        yield Path(directory)


def _engine(tmp_path, name="stand.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False, "timeout": 20})
    event.listen(engine, "connect", session_module.apply_sqlite_pragmas)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE user_sessions (id INTEGER PRIMARY KEY, token TEXT)"))
        conn.execute(text("CREATE TABLE evidence (id INTEGER PRIMARY KEY, v TEXT)"))
    return engine


def test_pragmas_are_applied_to_sqlite_connections(tmp_path):
    engine = _engine(tmp_path)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() >= 30000
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1  # NORMAL
    assert session_module.SQLITE_BUSY_TIMEOUT_MS >= 30000


def test_login_write_waits_for_a_long_worker_write_instead_of_failing(tmp_path):
    engine = _engine(tmp_path)
    worker_started = threading.Event()

    def worker():
        with engine.connect() as conn:
            conn.execute(text("INSERT INTO evidence (v) VALUES ('long')"))  # takes the write lock
            worker_started.set()
            time.sleep(7)  # longer than the old 5 s default timeout
            conn.commit()

    thread = threading.Thread(target=worker)
    thread.start()
    assert worker_started.wait(10)
    # status reads work while the worker writes (WAL)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM evidence")).scalar() == 0
    started = time.monotonic()
    with engine.begin() as conn:  # the login: a write into user_sessions
        conn.execute(text("INSERT INTO user_sessions (token) VALUES ('t')"))
    waited = time.monotonic() - started
    thread.join()
    assert waited > 4.0, "the write should have waited for the worker, not run beside it"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM user_sessions")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM evidence")).scalar() == 1


def test_release_write_lock_frees_the_lock_and_keeps_loaded_objects(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    worker = Session()
    worker.execute(text("INSERT INTO evidence (v) VALUES ('stage-1')"))
    worker.flush()
    other = create_engine(f"sqlite:///{tmp_path / 'stand.db'}", connect_args={"timeout": 0.2})
    try:
        with other.connect() as conn:
            conn.execute(text("PRAGMA busy_timeout=200"))
            try:
                conn.execute(text("INSERT INTO user_sessions (token) VALUES ('blocked')"))
                blocked = False
            except Exception:
                blocked = True
            conn.rollback()
        assert blocked, "the worker's open write transaction must hold the lock before the stage boundary"

        session_module.release_write_lock(worker)
        assert worker.expire_on_commit is True  # restored

        with other.begin() as conn:
            conn.execute(text("PRAGMA busy_timeout=200"))
            conn.execute(text("INSERT INTO user_sessions (token) VALUES ('free')"))
    finally:
        other.dispose()
        worker.close()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT v FROM evidence")).scalar() == "stage-1"


def test_release_write_lock_is_a_no_op_for_other_dialects():
    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _Db:
        def get_bind(self):
            return _Bind()

        def commit(self):
            raise AssertionError("must not commit on non-sqlite")

    session_module.release_write_lock(_Db())
