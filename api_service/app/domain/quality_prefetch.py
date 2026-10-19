"""Parallel warm-up of the per-page quality cache (`page_quality_cache`).

`official_evidence._quality_for_page` is a pure function of (document bytes, page) and its result is persisted on disk;
the pipeline calls it serially, in the parent, for every candidate page (one text-layer extraction, image placements
and, for textless pages, a full-page OCR each -- about a quarter of a cold run on a scan-heavy object). This module
computes the pages that are not cached yet in a spawn pool, one task per document, and lets those workers write the
cache; the parent's normal pass then finds every page on disk and returns exactly what it would have computed itself.
Which pages are assessed, in which order and with which result is untouched -- only who computes them.

No cache directory configured, a pool that cannot start, a worker that fails, a replaced page source (tests) = the
parent computes the page itself as before. `CASE10_QUALITY_PREFETCH=0` switches it off.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
import logging
import multiprocessing
import os
from types import SimpleNamespace
from typing import Any, Iterable

from . import page_quality_cache

logger = logging.getLogger(__name__)

_MIN_PAGES = 24      # below this the pool's start-up costs more than it saves


def is_enabled() -> bool:
    return os.getenv("CASE10_QUALITY_PREFETCH", "1").strip().lower() not in ("0", "false", "no", "off")


def _document_ref(document: Any) -> tuple[str, str] | None:
    from . import dataset_sources

    return dataset_sources.document_original_ref(document)


def _worker_count(tasks: int) -> int:
    from ..config import settings
    from .live_tagger_scan import available_cpu_count

    requested = settings.LIVE_TAGGER_WORKERS
    workers = requested if requested > 0 else min(available_cpu_count(), max(1, settings.LIVE_TAGGER_MAX_WORKERS))
    return max(1, min(workers, tasks))


def run_quality_task(task: dict[str, Any]) -> dict[str, Any]:
    """Worker entry point: assess the pages of one document; `_quality_for_page` stores each result on disk."""
    from . import dataset_sources
    from .official_evidence import _quality_for_page

    document = SimpleNamespace(
        dataset_metadata={"document_manifest": {"relative_path": task["relative"]}},
        file_hash=task["sha256"], content_hash=task["sha256"],
    )
    done = 0
    try:
        for page in task["pages"]:
            _quality_for_page(document, int(page))
            done += 1
    except Exception as exc:  # noqa: BLE001 -- the parent assesses whatever is still missing
        return {"failed": type(exc).__name__, "done": done}
    finally:
        dataset_sources._original_document_bytes.cache_clear()
        dataset_sources._ocr_page_words.cache_clear()
    return {"done": done}


def prefetch(pages: Iterable[tuple[Any, int]]) -> dict[str, Any]:
    """Warm the cache for `(document, page)` pairs. Returns small diagnostics; never raises."""
    report: dict[str, Any] = {"requested": 0, "missing": 0, "workers": 0}
    try:
        return _prefetch(pages, report)
    except Exception as exc:  # noqa: BLE001 -- an optimisation must never fail a run
        logger.warning("quality prefetch skipped: %s: %s", type(exc).__name__, exc)
        report["skipped"] = type(exc).__name__
        return report


def _prefetch(pages: Iterable[tuple[Any, int]], report: dict[str, Any]) -> dict[str, Any]:
    from . import dataset_sources, official_evidence
    from .live_candidate_tagger import _page_source_replaced

    if (not is_enabled() or page_quality_cache.cache_dir() is None or multiprocessing.parent_process() is not None
            or _page_source_replaced()):
        return report
    ocr_engine, ocr_lang = str(dataset_sources.settings.OCR_ENGINE), str(dataset_sources.settings.OCR_LANG)
    by_doc: dict[tuple[str, str], list[int]] = {}
    seen: set[tuple[str, str, int]] = set()
    for document, page in pages:
        ref = _document_ref(document)
        if ref is None or int(page) < 1 or (ref[0], ref[1], int(page)) in seen:
            continue
        seen.add((ref[0], ref[1], int(page)))
        report["requested"] += 1
        if (ref[0], ref[1], int(page)) in official_evidence._QUALITY_PAGE_CACHE:
            continue
        if page_quality_cache.load(ref[1], int(page), ocr_engine, ocr_lang) is not None:
            continue
        by_doc.setdefault(ref, []).append(int(page))
    report["missing"] = sum(len(v) for v in by_doc.values())
    if report["missing"] < _MIN_PAGES:
        return report
    tasks = [{"relative": rel, "sha256": sha, "pages": sorted(pgs)} for (rel, sha), pgs in by_doc.items()]
    tasks.sort(key=lambda task: (-len(task["pages"]), task["relative"], task["sha256"]))
    workers = _worker_count(len(tasks))
    report["workers"] = workers
    if workers <= 1:
        return report
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = [pool.submit(run_quality_task, task) for task in tasks]
            for future in as_completed(futures):
                outcome = future.result()
                if outcome.get("failed"):
                    report["failed_tasks"] = report.get("failed_tasks", 0) + 1
    except Exception as exc:  # noqa: BLE001 -- BrokenProcessPool, OSError...: the parent finishes the pages itself
        logger.warning("quality prefetch pool failed (%s: %s); the parent assesses the remaining pages", type(exc).__name__, exc)
        report["pool_error"] = type(exc).__name__
    return report
