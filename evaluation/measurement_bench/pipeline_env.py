"""Environment for running the REAL pipeline modules on the bench objects.

`setup(obj)` returns a session on a persistent sqlite DB (outside the repo) with
the object's documents imported (manifest rows only -- NO gold, NO organiser
annotations, so every object is measured in the same "live tagger only" regime),
with the byte source patched to the extracted originals.

The api_service package is imported from the repo tree as it is at call time; the
code fingerprint is recorded so a number can always be tied to the code it came
from (other prompts -- B, C, E1 -- edit the same tree concurrently).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .common import (
    BENCH_DIR, CORPUS_DIR, DB_DIR, MANIFESTS, NEW_OBJECTS_ROOT, OBJECTS, OUT_DIR, PUBLIC_DOCS_ROOT, REPO_ROOT, WORK_ROOT, sha256_file, utcnow_iso,
)

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ["CASE10_LLM_VERIFIER_ENABLED"] = "0"     # production default, pinned so results are reproducible
os.environ.setdefault("CASE10_LIVE_TAGGER_CACHE_DIR", str(WORK_ROOT / "tagger_cache"))
# The bench can be pointed at a frozen private copy of the api_service tree (CASE10_BENCH_APP_ROOT = a dir that
# contains `app/`): other prompts edit the live tree concurrently (no git), so every number is measured on one
# fixed snapshot whose file hashes are recorded in the outputs.
APP_ROOT = Path(os.environ.get("CASE10_BENCH_APP_ROOT", str(REPO_ROOT / "api_service")))
for p in (str(REPO_ROOT), str(APP_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def require_frozen_corpus() -> dict:
    """The stand refuses to run an extractor before the corpus is frozen and
    unmodified (values are written BEFORE the extractor runs)."""
    manifest = CORPUS_DIR / "corpus_manifest.json"
    if not manifest.exists():
        raise SystemExit("corpus_manifest.json missing: freeze the corpus first (build_corpus --freeze)")
    m = json.loads(manifest.read_text(encoding="utf-8"))
    if sha256_file(CORPUS_DIR / "value_corpus_v1.jsonl") != m["corpus_sha256"]:
        raise SystemExit("value_corpus_v1.jsonl changed after freeze")
    return m


def code_fingerprint() -> dict[str, Any]:
    base = APP_ROOT / "app"
    files = sorted(base.rglob("*.py"))
    per = {str(f.relative_to(base)).replace("\\", "/"): hashlib.sha256(f.read_bytes()).hexdigest()[:12] for f in files if "__pycache__" not in f.parts}
    total = hashlib.sha256(json.dumps(per, sort_keys=True).encode()).hexdigest()[:16]
    return {"combined": total, "n_files": len(per), "files": per}


def object_root(obj: str) -> Path | None:
    info = OBJECTS[obj]
    return NEW_OBJECTS_ROOT / info["dirname"] if info["kind"] == "new" else PUBLIC_DOCS_ROOT / obj


def patch_bytes(obj: str) -> None:
    """Point the byte source at the object's extracted originals.

    Preferred: the `CASE10_ORIGINALS_ROOT` environment override of dataset_sources (inherited by the tagger's spawned
    workers, SHA-256 still verified).  Fallback for a code tree without it: in-process monkeypatch (the tagger then
    stays in-process, see live_candidate_tagger._REAL_ORIGINAL_BYTES)."""
    import app.domain.dataset_sources as ds

    root = object_root(obj)
    src = Path(ds.__file__).read_text(encoding="utf-8")
    if "CASE10_ORIGINALS_ROOT" in src:
        os.environ["CASE10_ORIGINALS_ROOT"] = str(root)
        for name in ("_original_document_bytes", "_original_page_snapshots"):
            fn = getattr(ds, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
        return
    real = ds.original_document_bytes

    def patched(document):
        meta = document.dataset_metadata or {}
        row = meta.get("document_manifest") or meta.get("files_index") or {}
        rel = str(row.get("relative_path") or row.get("source_relative_path") or "").replace("/", "\\")
        path = root / rel
        if path.is_file():
            return path.read_bytes()
        if OBJECTS[obj]["kind"] == "public":
            return real(document)
        raise FileNotFoundError(str(path))

    ds.original_document_bytes = patched


def db_path(obj: str, tag: str = "base") -> Path:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return DB_DIR / f"{obj}_{tag}.sqlite"


def open_db(obj: str, *, fresh: bool = False, tag: str = "base"):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base

    path = db_path(obj, tag)
    if fresh and path.exists():
        path.unlink()
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}", connect_args={"timeout": 120})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), engine


def import_object(db, obj: str) -> str:
    from app.domain.official_dataset import _import_document_manifest, import_official_dataset

    info = OBJECTS[obj]
    object_id = info["object_id"]
    if info["kind"] == "new":
        _import_document_manifest(db, MANIFESTS / f"manifest_{obj}.jsonl", project_id=1, organization_id=1, object_ids={object_id})
    else:
        import_official_dataset(
            db, project_id=1, organization_id=1, object_ids=[object_id], include_pages=False,
        )
    db.flush()
    return object_id


def setup(obj: str, *, fresh: bool = True, tag: str = "base"):
    """(db, engine, object_id, process, docs, params) with documents imported, bytes patched."""
    from app.domain.v3_pipeline import (
        MATRIX_VERSION_OFFICIAL, _project_documents, create_process, current_document_versions, list_active_params,
    )

    patch_bytes(obj)
    db, engine = open_db(obj, fresh=fresh, tag=tag)
    object_id = import_object(db, obj)
    process = create_process(db, project_id=1, organization_id=1, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
    params = list_active_params(db, organization_id=1, project_id=1, matrix_version=MATRIX_VERSION_OFFICIAL)
    docs = current_document_versions(_project_documents(db, process))
    return db, engine, object_id, process, docs, params
