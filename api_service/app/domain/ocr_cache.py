"""Optional on-disk cache of full-page / clip OCR results (`dataset_sources._ocr_page_words`, `_ocr_original_clip`).

OCR of a page is a pure function of (page bytes, language, engine, render/threshold constants, Tesseract build) and costs
seconds; the in-process LRU in `dataset_sources` is lost with every process, so the quality workers, the parent's rule
packs and every re-run each OCR'd the same page again. The key is content-addressed (document SHA-256 + page), plus an
identity of everything else the answer depends on: the engine and languages, the installed Tesseract version and the hash
of `dataset_sources.py` itself (any edit of the OCR code invalidates the cache). Failures (`None`, empty clip text) are
never stored. Atomic writes; a corrupt or foreign file reads as a miss; no directory configured = no cache.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = 1


def cache_dir() -> Path | None:
    explicit = os.getenv("CASE10_OCR_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    tagger = os.getenv("CASE10_LIVE_TAGGER_CACHE_DIR", "").strip()
    return Path(tagger) / "ocr" if tagger else None


@lru_cache(maxsize=1)
def _tesseract_version() -> str | None:
    try:
        result = subprocess.run(["tesseract", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    text = result.stdout.decode("utf-8", errors="replace").strip()
    return text.splitlines()[0] if result.returncode == 0 and text else None


@lru_cache(maxsize=1)
def _identity() -> str | None:
    """None = do not cache (the OCR build cannot be identified)."""
    from ..config import settings
    from . import dataset_sources

    version = _tesseract_version()
    if version is None:
        return None
    try:
        code = hashlib.sha256(Path(dataset_sources.__file__).read_bytes()).hexdigest()
    except OSError:
        return None
    return json.dumps([_SCHEMA, str(settings.OCR_ENGINE), str(getattr(settings, "OCR_PADDLE_LANG", "")), version, code],
                      ensure_ascii=False)


def _path(directory: Path, kind: str, parts: list[Any]) -> Path | None:
    identity = _identity()
    if identity is None:
        return None
    key = hashlib.sha256(json.dumps([identity, kind, parts], ensure_ascii=False).encode("utf-8")).hexdigest()
    return directory / key[:2] / f"{key}.json"


def _read(path: Path | None, kind: str) -> Any:
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != _SCHEMA or record.get("kind") != kind:
        return None
    return record.get("value")


def _write(path: Path | None, kind: str, value: Any) -> None:
    if path is None:
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"schema": _SCHEMA, "kind": kind, "value": value}, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as exc:     # a cache that cannot be written never fails a run
        logger.warning("ocr cache write failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def load_words(sha256: str, page: int, lang: str) -> tuple[dict, ...] | None:
    directory = cache_dir()
    if directory is None:
        return None
    value = _read(_path(directory, "words", [sha256, int(page), lang]), "words")
    if not isinstance(value, list) or not all(isinstance(word, dict) and "text" in word and "bbox" in word for word in value):
        return None
    return tuple(value)


def store_words(sha256: str, page: int, lang: str, words: tuple[dict, ...]) -> None:
    directory = cache_dir()
    if directory is not None:
        _write(_path(directory, "words", [sha256, int(page), lang]), "words", [dict(word) for word in words])


def load_clip(sha256: str, page: int, clip: tuple[float, ...], lang: str) -> str | None:
    directory = cache_dir()
    if directory is None:
        return None
    value = _read(_path(directory, "clip", [sha256, int(page), list(clip), lang]), "clip")
    return value if isinstance(value, str) and value else None


def store_clip(sha256: str, page: int, clip: tuple[float, ...], lang: str, text: str) -> None:
    directory = cache_dir()
    if directory is not None and text:
        _write(_path(directory, "clip", [sha256, int(page), list(clip), lang]), "clip", text)
