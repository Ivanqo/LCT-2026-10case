"""Optional on-disk cache of `official_evidence._quality_for_page` results.

Key = SHA-256 of the PDF + page number + algorithm version + OCR engine and language (+ PyMuPDF version, which
decides the text-layer words the check reads). Same layout idea as the live-tagger / explication caches: atomic
write (temp file + rename), corrupt or foreign files read as a miss, a cache that cannot be written never fails a run.
No directory configured = no cache. Results that record an environment failure (OCR unavailable, a read error) are
never stored, so the cache cannot freeze a broken environment.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bump whenever the decision logic of `_quality_for_page` changes (reasons, thresholds, geometry predicates).
QUALITY_ALGORITHM_VERSION = "page-quality-v2"
_SCHEMA = 1


def cache_dir() -> Path | None:
    explicit = os.getenv("CASE10_QUALITY_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    tagger = os.getenv("CASE10_LIVE_TAGGER_CACHE_DIR", "").strip()
    return Path(tagger) / "quality" if tagger else None


def _cache_key(ocr_engine: str, ocr_lang: str) -> str:
    try:
        import fitz

        pymupdf = str(getattr(fitz, "VersionBind", "") or "")
    except Exception:
        pymupdf = ""
    payload = json.dumps([QUALITY_ALGORITHM_VERSION, str(ocr_engine), str(ocr_lang), pymupdf], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _path(directory: Path, sha256: str, page: int, key: str) -> Path:
    safe_sha = "".join(ch for ch in str(sha256) if ch.isalnum())[:64] or "nosha"
    return directory / safe_sha[:2] / f"{safe_sha}_p{int(page)}_{key}.json"


def is_cacheable(result: dict[str, Any]) -> bool:
    reasons = [str(reason) for reason in result.get("reasons") or []]
    return not any(reason.endswith("_OCR_UNAVAILABLE") or reason.startswith("PAGE_QUALITY_READ_FAILED")
                   for reason in reasons)


def load(sha256: str, page: int, ocr_engine: str, ocr_lang: str) -> dict[str, Any] | None:
    directory = cache_dir()
    if directory is None:
        return None
    key = _cache_key(ocr_engine, ocr_lang)
    try:
        record = json.loads(_path(directory, sha256, page, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or record.get("schema") != _SCHEMA or record.get("key") != key
            or record.get("sha256") != sha256 or record.get("page") != int(page)):
        return None
    result = record.get("result")
    if (not isinstance(result, dict) or result.get("page") != int(page)
            or result.get("quality_status") not in ("OK", "LOW_QUALITY")
            or not isinstance(result.get("reasons"), list)):
        return None
    return result


def store(sha256: str, page: int, ocr_engine: str, ocr_lang: str, result: dict[str, Any]) -> bool:
    directory = cache_dir()
    if directory is None or not is_cacheable(result):
        return False
    key = _cache_key(ocr_engine, ocr_lang)
    path = _path(directory, sha256, page, key)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"schema": _SCHEMA, "key": key, "sha256": sha256, "page": int(page),
                                   "result": result}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("page quality cache write failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
