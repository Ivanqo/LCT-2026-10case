"""Shared table input of the table-based detectors (UNMATCHED_DIFF, SEMANTIC_DISSONANCE).

The PD <-> RD room / apartment schedules are parsed by `table_parser` and paired by `explication_compare` (both pure
functions of the PDF text layer). This module owns only the *scan cache*: results are stored per file SHA-256 under a
fingerprint of the files that decide the parse (`table_parser`, `dataset_sources`, `anchor_search`), so the hypotheses'
cache does not go cold whenever the matrix comparison (`explication_compare`) is edited.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .. import explication_compare as ec
from .. import table_parser as tpar

logger = logging.getLogger(__name__)

_FINGERPRINT_FILES = ("table_parser.py", "dataset_sources.py", "anchor_search.py")


@lru_cache(maxsize=1)
def scan_fingerprint() -> str:
    base = Path(ec.__file__).resolve().parent
    digest = hashlib.sha256()
    for name in _FINGERPRINT_FILES:
        digest.update(name.encode())
        path = base / name
        digest.update(path.read_bytes() if path.is_file() else b"-")
    return digest.hexdigest()[:16]


def _cache_dir() -> Path | None:
    explicit = os.getenv("CASE10_FREE_HYPOTHESES_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    tagger = os.getenv("CASE10_LIVE_TAGGER_CACHE_DIR", "").strip()
    return Path(tagger) / "free_hypotheses_tables" if tagger else None


def _load(sha256: str) -> dict[str, Any] | None:
    directory = _cache_dir()
    if directory is None:
        return None
    try:
        record = json.loads((directory / f"{sha256}_{scan_fingerprint()}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record.get("result") if isinstance(record, dict) and record.get("fingerprint") == scan_fingerprint() else None


def _store(sha256: str, result: dict[str, Any]) -> None:
    directory = _cache_dir()
    if directory is None or result.get("skipped"):
        return
    path = directory / f"{sha256}_{scan_fingerprint()}.json"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"fingerprint": scan_fingerprint(), "result": result}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:  # a cache that cannot be written never fails a run
        logger.warning("free_hypotheses table cache write failed: %s", exc)


def scan_tables(documents: list[Any]) -> dict[int, dict[str, Any]]:
    """{document id: {"pages_total", "candidate_pages", "tables": [RoomTable dict]}}; uncached PDFs go through the
    bounded worker pool of `explication_compare.scan_documents_tables`."""
    from .. import dataset_sources

    out: dict[int, dict[str, Any]] = {}
    pending: list[tuple[Any, str]] = []
    for document in documents:
        ref = dataset_sources.document_original_ref(document)
        cached = _load(ref[1]) if ref else None
        if cached is not None:
            out[int(document.id)] = cached
        else:
            pending.append((document, ref[1] if ref else ""))
    if pending:
        scanned = ec.scan_documents_tables([document for document, _ in pending])
        for document, sha256 in pending:
            result = scanned[int(document.id)]
            out[int(document.id)] = result
            if sha256:
                _store(sha256, result)
    return out


def build_refs(documents: list[Any], gate: Any) -> tuple[dict[str, list[ec.TableRef]], dict[str, Any]]:
    """Same output as `explication_compare.build_table_refs`, over this module's scan cache."""
    refs: dict[str, list[ec.TableRef]] = {stage: [] for stage in ec._STAGES}
    diagnostics: dict[str, Any] = {"documents": 0, "pages_total": 0, "candidate_pages": 0, "tables": 0, "skipped": {}}
    scans = scan_tables(documents)
    for doc in documents:
        result = scans[int(doc.id)]
        diagnostics["documents"] += 1
        diagnostics["pages_total"] += int(result.get("pages_total") or 0)
        diagnostics["candidate_pages"] += len(result.get("candidate_pages") or [])
        if result.get("skipped"):
            diagnostics["skipped"][str(getattr(doc, "dataset_file_id", None) or doc.id)] = result["skipped"]
        fact = gate.facts.get(int(doc.id)) if gate is not None else None
        stage = ec._doc_stage(doc)
        for data in result.get("tables") or []:
            table = tpar.RoomTable.from_dict(data)
            refs[stage].append(ec.TableRef(
                table=table, stage=stage, file_id=str(getattr(doc, "dataset_file_id", None) or f"doc:{doc.id}"), doc_key=int(doc.id),
                revision_rank=ec._revision_rank(fact), document=doc, volume=getattr(fact, "section_key", None)))
            diagnostics["tables"] += 1
    return refs, diagnostics


def paired_tables(documents: Iterable[Any], gate: Any) -> tuple[list[ec.PairResult], dict[str, Any], dict[str, list[ec.TableRef]]]:
    """Table pairs (PD reference, RD table) of the object's АР documents with their row-level differences, and all parsed
    tables per stage."""
    candidates = ec.candidate_documents(list(documents), gate)
    refs, diagnostics = build_refs(candidates, gate)
    results = ec.compare_pairs(refs["PD"], refs["RD"], conflicting=ec._conflicts(gate, candidates), policy=ec.revision_policy())
    diagnostics["pairs"] = len(results)
    diagnostics["candidate_files"] = [str(getattr(d, "dataset_file_id", None) or d.id) for d in candidates]
    return results, diagnostics, refs


# ---------------------------------------------------------------- evidence helpers shared by the table detectors


def row_evidence(ref: ec.TableRef, row: Any, *, role: str, value: str | None, confidence: float) -> Any:
    from .base import EvidenceRef

    return EvidenceRef(document=ref.document, page=ref.page, stage=ref.stage, role=role, bbox_pdf=list(row.bbox),
                       page_width=ref.table.width, page_height=ref.table.height, value=value,
                       context=ec._row_summary(ref.table, row), confidence=confidence)


def table_evidence(ref: ec.TableRef, *, role: str, value: str | None, confidence: float) -> Any:
    from .base import EvidenceRef

    return EvidenceRef(document=ref.document, page=ref.page, stage=ref.stage, role=role, bbox_pdf=list(ref.table.bbox),
                       page_width=ref.table.width, page_height=ref.table.height, value=value,
                       context=f"{ref.table.title or 'таблица'} ({len(ref.table.rows)} строк)", confidence=confidence)


def scope_label(result: ec.PairResult) -> str:
    return result.rd.table.scope.label() or result.pd.table.scope.label() or ""


def ranking_confidence(result: ec.PairResult, d: ec.Discrepancy) -> float:
    """A ranking score for the inspector's queue, built from how the rows were matched and whether the table scope
    (floor) is known on both sides. It is not a probability of a violation."""
    score = float(d.confidence)
    if result.alternatives:
        score -= 0.1
    if not (result.pd.table.scope.floor and result.rd.table.scope.floor):
        score -= 0.1
    return round(min(0.95, max(0.2, score)), 3)
