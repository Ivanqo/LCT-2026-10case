"""Per-document scan primitives for the live candidate tagger: the DB-free,
picklable-in/picklable-out half of `live_candidate_tagger.py`.

`live_candidate_tagger.tag_live_candidates` splits a tagging run into three
phases so its output is a pure function of the object's documents and never of
machine speed, load or thread count:

1. **plan** (parent, deterministic): which documents are scanned, in which
   order, with which page budget -- decided before any work starts;
2. **scan** (this module, parallelisable): for each planned document, open its
   pages and record every anchor-phrase hit (`scan_document`). Pure function of
   (file bytes, page range, anchor list) -- no DB, no clock, no shared state --
   so it can run in a spawned worker process or in the parent and gives the
   same answer; its result is also what the on-disk cache stores;
3. **merge** (parent, deterministic): apply the results in plan order --
   semantic confirmation, `SourceFragment` writes, coverage markers.

Nothing here decides *what* to scan; that is the plan phase's job.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, Callable

from .anchor_search import (
    cluster_rows,
    find_anchor_end_index_for_phrase,
    row_text_at_word,
    union_bbox,
)

logger = logging.getLogger(__name__)

# Bump when the SHAPE of a scan result changes; content changes of the code that
# produces it are caught separately by `scan_code_fingerprint`.
SCAN_RESULT_SCHEMA = 1

# Source files whose logic determines a scan result. Their content hash is part
# of every cache key, so editing any of them silently invalidates the cache
# instead of serving results produced by older logic (this project is not under
# version control; a stale-cache bug would be invisible).
_FINGERPRINT_MODULES = (
    "anchor_search.py",
    "dataset_sources.py",
    "matrix_unit_classifier.py",
    "live_tagger_scan.py",
    "ocr_paddle.py",
)


@lru_cache(maxsize=1)
def scan_code_fingerprint() -> str:
    digest = hashlib.sha256()
    base = Path(__file__).resolve().parent
    for name in _FINGERPRINT_MODULES:
        path = base / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b"-")
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------- CPU count


def _cgroup_cpu_limit() -> float | None:
    """CPU quota of the container this process runs in (docker `--cpus`), or
    `None` when unlimited/unreadable. `os.cpu_count()`/affinity report the
    HOST's CPUs, which would oversubscribe a quota-limited container."""
    try:  # cgroup v2
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def available_cpu_count() -> int:
    """CPUs this process may actually use: affinity mask where the platform has
    one, else `os.cpu_count()`, further bounded by a cgroup CPU quota."""
    try:
        count = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        count = os.cpu_count() or 1
    limit = _cgroup_cpu_limit()
    if limit is not None:
        count = min(count, max(1, math.floor(limit)))
    return max(1, count)


# ---------------------------------------------------------------- scan


def scan_document(
    document: Any,
    page_numbers: list[int],
    anchors: list[tuple[str, str]],
    extract_pages: Callable[[Any, list[int]], dict[int, dict]],
) -> dict[str, Any]:
    """Every anchor-phrase hit on `page_numbers` of `document`.

    `anchors` is an ORDERED list of `(code, phrase)`; hits come out sorted by
    (page ascending, then anchor order), the order the merge phase replays.
    A hit is `{page, code, row_text, bbox_pdf, width, height}` -- the matched
    visual row and its union bbox, exactly what the tagger used to compute
    inline. The semantic confirmation step is deliberately NOT here (it needs
    the embedding model and is applied in the parent, in order)."""
    snapshots = extract_pages(document, list(page_numbers))
    hits: list[dict[str, Any]] = []
    pages_no_text = 0
    corruption_suspected = 0
    for page_number in sorted(snapshots):
        snapshot = snapshots[page_number]
        if snapshot.get("cmap_corruption_suspected"):
            corruption_suspected += 1
        words = snapshot.get("words") or []
        if not words:
            pages_no_text += 1
            continue
        rows = cluster_rows(words)
        width = float(snapshot.get("width") or 0) or None
        height = float(snapshot.get("height") or 0) or None
        for code, phrase in anchors:
            idx = find_anchor_end_index_for_phrase(words, phrase)
            if idx is None or idx <= 0:
                continue
            anchor_word = words[idx - 1]
            row = next((r for r in rows if any(w is anchor_word for w in r)), None)
            hits.append({
                "page": int(page_number),
                "code": code,
                "row_text": row_text_at_word(words, idx - 1),
                "bbox_pdf": union_bbox([w.get("bbox") for w in row]) if row else None,
                "width": width,
                "height": height,
            })
    return {
        "schema": SCAN_RESULT_SCHEMA,
        "pages_requested": len(page_numbers),
        "pages_scanned": len(snapshots),
        "pages_no_text": pages_no_text,
        # A page whose broken-font text layer could not be OCR-recovered (OCR
        # engine unavailable) yields an environment-dependent result -- never
        # cache it, or a later run with a working OCR engine would be served
        # the degraded answer.
        "cacheable": corruption_suspected == 0,
        "hits": hits,
    }


def run_scan_task(task: dict[str, Any]) -> dict[str, Any]:
    """Worker-process entry point (top-level so it pickles by name). `task` is
    plain data: `{relative, sha256, pages, anchors}`. Never raises -- a failing
    document comes back as `{"failed": "<ExceptionType>"}` so one bad PDF cannot
    take down the pool; the parent retries it once in-process before recording
    the failure."""
    from . import dataset_sources

    document = SimpleNamespace(
        dataset_metadata={"document_manifest": {"relative_path": task["relative"]}},
        file_hash=task["sha256"],
        content_hash=task["sha256"],
    )
    cpu_started = time.process_time()
    try:
        result = scan_document(document, task["pages"], task["anchors"], dataset_sources.extract_original_pages)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        result = {"failed": type(exc).__name__, "detail": str(exc)[:200]}
    # CPU time of this task, for measurement only (a load-insensitive cost figure --
    # wall-clock on a shared box is not): the parent pops it before the result is
    # cached or merged, so it can never reach anything that must be reproducible.
    result["_cpu_seconds"] = time.process_time() - cpu_started
    try:
        return result
    finally:
        # A long-lived worker would otherwise pin up to 4 whole PDFs and 32
        # page-snapshot sets in these process-local LRUs -- useless here (each
        # document is opened exactly once) and multiplied by the worker count.
        dataset_sources._original_document_bytes.cache_clear()
        dataset_sources._original_page_snapshots.cache_clear()


def init_worker() -> None:
    """Pool initializer: keep children out of the way of the API process."""
    try:
        os.nice(5)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


# ---------------------------------------------------------------- cache


def cache_key(sha256: str, stage: str, anchors: list[tuple[str, str]], page_count: int, extra: dict[str, Any]) -> str:
    """Identity of a scan result. Everything that can change the answer for
    identical file bytes is in it: the file (SHA-256), the anchor list actually
    searched (so a catalog edit invalidates it), the scanned page range, the
    caller-supplied scan settings (OCR engine/language, semantic-anchor
    fallback), and the fingerprint of the scan code itself."""
    payload = json.dumps(
        {
            "schema": SCAN_RESULT_SCHEMA,
            "code": scan_code_fingerprint(),
            "sha256": sha256,
            "stage": stage,
            "pages": int(page_count),
            "anchors": [[code, phrase] for code, phrase in anchors],
            "extra": extra,
        },
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(cache_dir: Path, sha256: str, key: str) -> Path:
    safe_sha = "".join(ch for ch in str(sha256) if ch.isalnum())[:64] or "nosha"
    return cache_dir / safe_sha[:2] / f"{safe_sha}_{key[:24]}.json"


def load_cached_scan(cache_dir: Path, sha256: str, key: str) -> dict[str, Any] | None:
    path = _cache_path(cache_dir, sha256, key)
    try:
        with path.open("r", encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return None
    # Guard against a truncated/foreign file: the record must name its own key.
    if not isinstance(record, dict) or record.get("key") != key or record.get("schema") != SCAN_RESULT_SCHEMA:
        return None
    result = record.get("result")
    return result if isinstance(result, dict) and "hits" in result else None


def store_cached_scan(cache_dir: Path, sha256: str, key: str, result: dict[str, Any]) -> bool:
    """Atomic write (temp file + rename) so a crash or a concurrent writer (the
    API and the RabbitMQ worker share the volume) can never leave a half-written
    file that a reader would take for a result. Best effort: a cache that cannot
    be written must never fail a tagging run."""
    if not result.get("cacheable", True) or "failed" in result:
        return False
    path = _cache_path(cache_dir, sha256, key)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump({"schema": SCAN_RESULT_SCHEMA, "key": key, "result": result}, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("live_tagger cache write failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
