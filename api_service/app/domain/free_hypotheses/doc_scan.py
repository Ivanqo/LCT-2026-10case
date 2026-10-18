"""Per-document page scan for the page-text detectors (CHANGE_SHEET, LAYER_COMPOSITION).

One pass over the pages of a PDF original (`get_text("text")`, cheap) decides which pages are worth the word-level extraction:

  changes   pages whose text holds a change register header («Содержание изменения»)      -> change register entries
  layers    pages with at least three construction-material words                         -> layer stacks (compositions)
  index     every page (PD only): stem -> page frequency, rare stems -> pages, dotted ids  -> the look-up for the PD side

The result is a pure function of the file bytes and of the scan code, so it is cached per SHA-256 under a fingerprint of the
code that decides it (`CASE10_FREE_HYPOTHESES_CACHE_DIR`, else `<tagger cache>/free_hypotheses_pages`); uncached PDFs go through a
bounded spawn pool exactly like `explication_compare.scan_documents_tables`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from . import change_sheet as cs
from . import layer_composition as lc
from .text_utils import STEM_LENGTH, fold, stems

logger = logging.getLogger(__name__)

KINDS = ("changes", "layers", "index")
_FINGERPRINT_FILES = ("free_hypotheses/doc_scan.py", "table_parser.py", "dataset_sources.py", "anchor_search.py", "free_hypotheses/text_utils.py")
_CHANGE_PREFILTER = re.compile(r"(содержани|описани)\w*\s+изменени", re.IGNORECASE)
_LAYER_WORDS = re.compile(
    r"(бетон|стяжк|утеплит|ват[аыу]\b|пенополи|пенопласт|мембран|плитк|пароизол|гидроизол|штукатур|кирпич|гипсокартон|ламинат|линолеум|"
    r"керамогранит|песчан|грунт|профнастил|битум|щебен|геотекстил|экструз|фанер|подготовк|засыпк|керамзит|облицовк|сэндвич|перлит|гравий|"
    r"мастик|рубероид|звукоизол|теплоизол|газобетон|пеноблок|армир)", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[а-яё]{4,}")
_DOTTED_RE = re.compile(r"(?<![\d.,-])\d{1,3}(?:\.\d{1,3}){2,3}(?![\d.]*\d)")
_MARK_RE = re.compile(r"(?<![А-Яа-яA-Za-z0-9])[А-ЯA-Z]{1,5}[-–]\d{1,3}[а-яa-z]?(?![\dА-Яа-яA-Za-z])")
INDEX_RARE_PAGES = 40
MAX_PAGES_PER_DOCUMENT = int(os.getenv("CASE10_FREE_HYPOTHESES_MAX_SCAN_PAGES", "12000"))


@lru_cache(maxsize=None)
def kind_fingerprint(kind: str) -> str:
    """Fingerprint of the code that decides one kind of scan result (parsers, prefilters, the readers of the page text); the
    matching code of the detectors is not part of it, so tuning a threshold or a vocabulary never makes a cache cold."""
    import inspect

    base = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256(kind.encode())
    for name in _FINGERPRINT_FILES:
        digest.update(name.encode())
        path = base / name
        digest.update(path.read_bytes() if path.is_file() else b"-")
    if kind == "changes":
        for function in (cs.parse_change_register, cs._find_header, cs._title_block_band):
            digest.update(inspect.getsource(function).encode())
        digest.update(repr((cs.MAX_MARKER_DISTANCE, _CHANGE_PREFILTER.pattern)).encode())
    elif kind == "layers":
        for function in (lc.parse_compositions, lc._layer_rows, lc._continuation, lc._segments):
            digest.update(inspect.getsource(function).encode())
        digest.update(repr((lc.MIN_LAYERS, lc.MAX_THICKNESS_MM, lc._SEGMENT_GAP, lc._LEFT_ALIGN, lc._ROW_GAP, lc._CONTINUATION_GAP, lc._MATERIAL_RE.pattern,
                            lc._EXPLICIT_RE.pattern, _LAYER_WORDS.pattern)).encode())
    else:
        digest.update(repr((_TOKEN_RE.pattern, _DOTTED_RE.pattern, _MARK_RE.pattern, INDEX_RARE_PAGES, STEM_LENGTH)).encode())
    return digest.hexdigest()[:16]


def cache_dir() -> Path | None:
    explicit = os.getenv("CASE10_FREE_HYPOTHESES_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit) / "pages"
    tagger = os.getenv("CASE10_LIVE_TAGGER_CACHE_DIR", "").strip()
    return Path(tagger) / "free_hypotheses_pages" if tagger else None


def _cache_path(sha256: str) -> Path | None:
    directory = cache_dir()
    return directory / f"{sha256}.json" if directory else None


def _read_record(sha256: str) -> dict[str, Any]:
    path = _cache_path(sha256)
    if path is None:
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _load(sha256: str) -> dict[str, Any]:
    """{kind: result} of the kinds whose stored fingerprint is the current one."""
    stored = _read_record(sha256).get("kinds") or {}
    return {kind: item["result"] for kind, item in stored.items() if kind in KINDS and isinstance(item, dict) and item.get("fp") == kind_fingerprint(kind)}


def _store(sha256: str, kinds: dict[str, Any]) -> None:
    path = _cache_path(sha256)
    if path is None:
        return
    merged = {k: v for k, v in (_read_record(sha256).get("kinds") or {}).items() if k not in kinds}
    merged.update({kind: {"fp": kind_fingerprint(kind), "result": result} for kind, result in kinds.items()})
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"kinds": merged}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:  # a cache that cannot be written never fails a run
        logger.warning("free_hypotheses page cache write failed: %s", exc)


def _scan_uncached(document: Any, kinds: Iterable[str]) -> dict[str, Any]:
    """{kind: result} for the requested kinds, from the PDF bytes."""
    from .. import dataset_sources
    import fitz

    kinds = [k for k in kinds if k in KINDS]
    try:
        data = dataset_sources.original_document_bytes(document)
    except Exception as exc:  # noqa: BLE001 -- an unreadable original is reported, never fatal
        return {kind: {"skipped": type(exc).__name__} for kind in kinds}
    change_pages: list[int] = []
    layer_pages: list[int] = []
    df: dict[str, int] = {}
    inv: dict[str, list[int]] = {}
    ids: dict[str, list[int]] = {}
    with fitz.open(stream=data, filetype="pdf") as pdf:
        total = len(pdf)
        if total > MAX_PAGES_PER_DOCUMENT:
            return {kind: {"skipped": "too_many_pages", "pages_total": total} for kind in kinds}
        for index in range(total):
            text = pdf[index].get_text("text")
            flat = re.sub(r"\s+", " ", text)
            if "changes" in kinds and _CHANGE_PREFILTER.search(flat):
                change_pages.append(index + 1)
            if "layers" in kinds and len({m.group(1).lower() for m in _LAYER_WORDS.finditer(flat)}) >= 3:
                layer_pages.append(index + 1)
            if "index" in kinds:
                page_stems = {w[:STEM_LENGTH] for w in _TOKEN_RE.findall(fold(flat))}
                for stem in page_stems:
                    df[stem] = df.get(stem, 0) + 1
                    inv.setdefault(stem, []).append(index + 1)
                for token in set(_DOTTED_RE.findall(flat)) | {m.replace("–", "-") for m in _MARK_RE.findall(flat)}:
                    ids.setdefault(token, []).append(index + 1)
    out: dict[str, Any] = {}
    if "changes" in kinds:
        entries: list[dict[str, Any]] = []
        low_quality: list[dict[str, Any]] = []
        if change_pages:
            snapshots = dataset_sources.extract_original_pages(document, change_pages)
            for page in sorted(snapshots):
                if snapshots[page].get("cmap_corruption_suspected"):
                    low_quality.append({"page": page, "reason": "broken_text_layer_no_ocr"})
                    continue
                entries.extend(cs.parse_change_register(snapshots[page], page=page))
        out["changes"] = {"pages_total": total, "candidate_pages": change_pages, "entries": entries, "low_quality": low_quality}
    if "layers" in kinds:
        compositions: list[dict[str, Any]] = []
        low_quality = []
        if layer_pages:
            snapshots = dataset_sources.extract_original_pages(document, layer_pages)
            for page in sorted(snapshots):
                if snapshots[page].get("cmap_corruption_suspected"):
                    low_quality.append({"page": page, "reason": "broken_text_layer_no_ocr"})
                    continue
                compositions.extend(lc.parse_compositions(snapshots[page], page=page))
        out["layers"] = {"pages_total": total, "candidate_pages": layer_pages, "compositions": compositions, "low_quality": low_quality}
    if "index" in kinds:
        out["index"] = {"pages_total": total, "df": df, "inv": {s: p for s, p in inv.items() if len(p) <= INDEX_RARE_PAGES},
                        "ids": {t: p for t, p in ids.items() if len(p) <= INDEX_RARE_PAGES}}
    return out


def _worker(task: dict[str, Any]) -> dict[str, Any]:
    from .. import dataset_sources

    document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": task["relative"]}}, file_hash=task["sha256"], content_hash=task["sha256"])
    try:
        return _scan_uncached(document, task["kinds"])
    except Exception as exc:  # noqa: BLE001 -- one unreadable PDF must not sink the object's run
        return {kind: {"skipped": type(exc).__name__} for kind in task["kinds"]}
    finally:
        dataset_sources._original_document_bytes.cache_clear()
        dataset_sources._original_page_snapshots.cache_clear()


def scan_documents(documents: list[Any], kinds: Iterable[str]) -> dict[int, dict[str, Any]]:
    """{document id: {kind: result}}; cached per SHA-256, uncached PDFs scanned in a bounded process pool (<= 3 workers)."""
    from .. import dataset_sources

    kinds = tuple(k for k in KINDS if k in set(kinds))
    result: dict[int, dict[str, Any]] = {}
    pending: list[tuple[Any, str, str, tuple[str, ...]]] = []
    for document in documents:
        ref = dataset_sources.document_original_ref(document)
        if ref is None:
            result[int(document.id)] = {kind: {"skipped": "no_pdf_original"} for kind in kinds}
            continue
        relative, sha256 = ref
        cached = _load(sha256)
        missing = tuple(k for k in kinds if k not in cached)
        if not missing:
            result[int(document.id)] = {k: cached[k] for k in kinds}
        else:
            pending.append((document, relative, sha256, missing))
    if pending:
        requested = os.getenv("CASE10_FREE_HYPOTHESES_WORKERS") or os.getenv("CASE10_EXPLICATION_WORKERS") or os.getenv("CASE10_LIVE_TAGGER_WORKERS") or "3"
        try:
            workers = min(3, max(1, int(requested)), len(pending))
        except ValueError:
            workers = min(3, len(pending))
        scanned: dict[int, dict[str, Any]] = {}
        remaining = {int(d.id): (d, r, s, m) for d, r, s, m in pending}
        if workers > 1:
            try:
                with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
                    futures = {pool.submit(_worker, {"relative": r, "sha256": s, "kinds": m}): int(d.id) for d, r, s, m in pending}
                    for future in as_completed(futures):
                        doc_id = futures[future]
                        try:
                            scanned[doc_id] = future.result()
                        except (BrokenProcessPool, OSError, RuntimeError, ImportError):
                            raise
                        except Exception as exc:  # noqa: BLE001
                            scanned[doc_id] = {kind: {"skipped": type(exc).__name__} for kind in remaining[doc_id][3]}
                        remaining.pop(doc_id, None)
            except (BrokenProcessPool, OSError, RuntimeError, ImportError) as exc:
                logger.warning("free_hypotheses page scan: worker pool failed (%s: %s); finishing %d documents in-process", type(exc).__name__, exc, len(remaining))
        for doc_id, (document, _r, _s, missing) in list(remaining.items()):
            try:
                scanned[doc_id] = _scan_uncached(document, missing)
            except Exception as exc:  # noqa: BLE001
                scanned[doc_id] = {kind: {"skipped": type(exc).__name__} for kind in missing}
        for document, _relative, sha256, missing in pending:
            fresh = scanned[int(document.id)]
            good = {k: v for k, v in fresh.items() if not v.get("skipped")}
            if good:
                _store(sha256, good)
            result[int(document.id)] = {k: (fresh.get(k) or _load(sha256).get(k) or {"skipped": "not_scanned"}) for k in kinds}
    return result
