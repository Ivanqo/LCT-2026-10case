"""Optional on-disk cache of whole-file SHA-256 digests, keyed by (absolute path, size, mtime_ns).

The package scan hashes every PDF once per run; on a mounted package (Docker Desktop on Windows) that is I/O bound and
a re-run of an unchanged package paid it again. A record is used only when path, size and modification time (ns) all
match the file as it is now; anything else -- or any unreadable/foreign record -- reads as a miss and the file is
hashed. Same layout as the other caches here: atomic write (temp file + rename), a cache that cannot be written never
fails a run, no directory configured = no cache. The digest returned is always that of the bytes the first full read
saw: a file rewritten in place with the same size AND the same mtime_ns would be served its old digest, which is the
accepted trade of any stat-keyed cache (the originals are re-verified against the manifest hash when pages are read).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_SCHEMA = 1


def cache_dir() -> Path | None:
    explicit = os.getenv("CASE10_FILE_HASH_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    tagger = os.getenv("CASE10_LIVE_TAGGER_CACHE_DIR", "").strip()
    return Path(tagger) / "file_sha256" if tagger else None


def _record_path(directory: Path, path: Path, size: int, mtime_ns: int) -> Path:
    key = hashlib.sha256(f"{path}|{size}|{mtime_ns}".encode("utf-8", errors="surrogatepass")).hexdigest()
    return directory / key[:2] / f"{key}.json"


def cached_sha256(path: Path, compute: Callable[[Path], str]) -> str:
    """SHA-256 of `path`: from the cache when the file is unchanged since it was last hashed, else `compute(path)`."""
    directory = cache_dir()
    if directory is None:
        return compute(path)
    try:
        resolved = Path(path).resolve()
        stat = resolved.stat()
        size, mtime_ns = int(stat.st_size), int(stat.st_mtime_ns)
        record_path = _record_path(directory, resolved, size, mtime_ns)
    except (OSError, ValueError):
        return compute(path)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        digest = record.get("sha256")
        if (isinstance(record, dict) and record.get("schema") == _SCHEMA and record.get("path") == str(resolved)
                and record.get("size") == size and record.get("mtime_ns") == mtime_ns
                and isinstance(digest, str) and len(digest) == 64):
            return digest
    except (OSError, ValueError):
        pass
    digest = compute(path)
    try:
        after = resolved.stat()
        if int(after.st_size) == size and int(after.st_mtime_ns) == mtime_ns:   # unchanged while we were reading it
            record_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = record_path.with_name(f"{record_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"schema": _SCHEMA, "path": str(resolved), "size": size, "mtime_ns": mtime_ns,
                                       "sha256": digest}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, record_path)
    except (OSError, ValueError) as exc:
        logger.warning("file hash cache write failed: %s", exc)
    return digest
