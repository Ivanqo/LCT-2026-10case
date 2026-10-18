"""Orchestration of the free-hypothesis detectors for one inspection process (TZ 9.5).

`run_free_hypotheses` is called from `logical_analysis.run_logical_analysis_module` (which `official_evidence` already
calls after the matrix comparison and before its orphan sweep). Steps: detectors -> de-duplication inside the object ->
per-document / per-object limits -> idempotent upsert (inspector reviews preserved) -> reconciliation of the previous run's
hypotheses -> one audit entry with the diagnostics.

A detector registers as `(method, run)`; `run(ctx)` returns `(candidates, stats, scope_complete)`. A method whose input is
partly out of this run's parameter scope reports `scope_complete=False` and its previous hypotheses are left untouched.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from ...db.models import AuditLog, DocumentVersion, EvidenceGroup, InspectionProcess, Param
from ...request_context import get_client_ip
from .base import (
    ALL_METHODS,
    METHOD_LOGICAL,
    METHOD_CHANGE_SHEET,
    METHOD_LAYERS,
    METHOD_ML,
    METHOD_NORMATIVE,
    METHOD_SEMANTIC,
    METHOD_UNMATCHED,
    HypothesisCandidate,
    apply_limits,
    enabled_methods,
    free_hypotheses_enabled,
    limit_per_document,
    limit_per_object,
    merge_duplicates,
    norm_token,
)
from .store import ensure_free_search_param, reconcile_stale, store_candidates

logger = logging.getLogger(__name__)
TABLE_METHODS = {METHOD_UNMATCHED, METHOD_SEMANTIC, METHOD_LOGICAL, METHOD_ML}


@dataclass
class RunContext:
    db: Session
    process: InspectionProcess
    params: list[Param]
    documents: list[DocumentVersion]
    object_id: str | None
    gate: Any = None
    results: list = field(default_factory=list)          # explication_compare.PairResult
    refs: dict = field(default_factory=dict)             # stage -> [TableRef]
    s1_reported: set = field(default_factory=set)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def codes(self) -> set[str]:
        return {str(p.code) for p in self.params}


def matrix_reported_keys(db: Session, process: InspectionProcess) -> set[tuple]:
    """Differences S1 (flag CASE10_EXPLICATION_COMPARE=1) already wrote as matrix findings: (location, field, type, PD, RD)."""
    out: set[tuple] = set()
    for group in db.query(EvidenceGroup).filter(EvidenceGroup.process_id == process.id, EvidenceGroup.group_key.like("explication:%")).all():
        delta = group.delta if isinstance(group.delta, dict) else {}
        values = delta.get("values") if isinstance(delta.get("values"), dict) else {}
        out.add((norm_token(delta.get("location")), str(delta.get("field")), str(delta.get("discrepancy_type")),
                 norm_token(values.get("PD")), norm_token(values.get("RD"))))
    return out


def _load_tables(ctx: RunContext) -> None:
    from .tables import paired_tables

    t0 = time.monotonic()
    ctx.results, table_diag, ctx.refs = paired_tables(ctx.documents, ctx.gate)
    table_diag["seconds"] = round(time.monotonic() - t0, 1)
    ctx.diagnostics["tables"] = table_diag
    ctx.s1_reported = matrix_reported_keys(ctx.db, ctx.process)


def _detect_unmatched(ctx: RunContext):
    from . import unmatched_diff

    out, stats = unmatched_diff.detect(ctx.results, object_id=ctx.object_id, already_reported=ctx.s1_reported)
    return out, stats, True


def _detect_semantic(ctx: RunContext):
    from . import semantic_dissonance

    out, stats = semantic_dissonance.detect(ctx.results, object_id=ctx.object_id, already_reported=ctx.s1_reported)
    return out, stats, True


def _detect_logical(ctx: RunContext):
    from . import logic_rules

    relations, rel_stats = logic_rules.detect_relations(ctx.db, ctx.process, ctx.codes, object_id=ctx.object_id)
    schedules, sched_stats = logic_rules.detect_schedule_rules(ctx.refs, object_id=ctx.object_id, db=ctx.db, process=ctx.process, params_in_scope=ctx.codes)
    return relations + schedules, {"relations": rel_stats, "schedules": sched_stats}, bool(rel_stats.get("scope_complete"))


def _detect_change_sheet(ctx: RunContext):
    from . import change_detect

    out, stats = change_detect.detect(ctx.documents, ctx.gate, object_id=ctx.object_id)
    return out, stats, True


def _detect_layers(ctx: RunContext):
    from . import layer_detect

    out, stats = layer_detect.detect(ctx.documents, ctx.gate, object_id=ctx.object_id)
    return out, stats, True


def _detect_normative(ctx: RunContext):
    from . import normative

    out, stats = normative.detect(ctx.db, ctx.process, ctx.params, object_id=ctx.object_id)
    return out, stats, True


def _detect_ml(ctx: RunContext):
    from . import ml_stat

    out, stats = ml_stat.detect(ctx.refs, object_id=ctx.object_id)
    return out, stats, True


DETECTORS: dict[str, Callable[[RunContext], tuple[list[HypothesisCandidate], dict[str, Any], bool]]] = {
    METHOD_UNMATCHED: _detect_unmatched,
    METHOD_CHANGE_SHEET: _detect_change_sheet,
    METHOD_LAYERS: _detect_layers,
    METHOD_SEMANTIC: _detect_semantic,
    METHOD_LOGICAL: _detect_logical,
    METHOD_NORMATIVE: _detect_normative,
    METHOD_ML: _detect_ml,
}


def collect_candidates(ctx: RunContext, methods: Iterable[str]) -> tuple[list[HypothesisCandidate], dict[str, Any], set[str]]:
    """Run the detectors; returns (all candidates, per-method stats, methods whose scope was complete)."""
    methods = [m for m in ALL_METHODS if m in set(methods)]
    stats: dict[str, Any] = {}
    candidates: list[HypothesisCandidate] = []
    complete: set[str] = set()
    if TABLE_METHODS & set(methods):
        try:
            _load_tables(ctx)
        except Exception:  # noqa: BLE001 -- a table-scan failure must not break the matrix run
            logger.exception("free_hypotheses: table scan failed")
            ctx.diagnostics["tables"] = {"error": "table_scan_failed"}
            ctx.results, ctx.refs = [], {"PD": [], "RD": []}
            methods = [m for m in methods if m not in TABLE_METHODS - {METHOD_LOGICAL}]
            ctx.diagnostics["abstained_methods"] = sorted(TABLE_METHODS - {METHOD_LOGICAL})
    for method in methods:
        t0 = time.monotonic()
        try:
            found, method_stats, scope_complete = DETECTORS[method](ctx)
        except Exception:  # noqa: BLE001 -- one detector failing leaves the others (and the matrix result) intact
            logger.exception("free_hypotheses: detector %s failed", method)
            stats[method] = {"error": "detector_failed"}
            continue
        method_stats["seconds"] = round(time.monotonic() - t0, 2)
        stats[method] = method_stats
        candidates.extend(found)
        if scope_complete:
            complete.add(method)
    return candidates, stats, complete


def run_free_hypotheses(db: Session, process: InspectionProcess, params: list[Param], touched_keys: dict[int, set[str]] | None = None, *,
                        by_id: dict[int, DocumentVersion], user_id: int | None = None, methods: Iterable[str] | None = None,
                        force: bool = False) -> dict[str, Any]:
    """Returns the diagnostics (also written to the audit log). `force` runs although CASE10_FREE_HYPOTHESES is off
    (tests, measurement scripts)."""
    if not (force or free_hypotheses_enabled()):
        return {"enabled": False}
    methods = tuple(methods) if methods is not None else enabled_methods()
    if not methods or not by_id:
        return {"enabled": True, "methods": list(methods), "documents": len(by_id), "skipped": "nothing_to_do"}
    from ..comparison_gate import GateContext

    t0 = time.monotonic()
    documents = sorted(by_id.values(), key=lambda d: int(d.id))
    ctx = RunContext(db=db, process=process, params=list(params), documents=documents, object_id=process.object_id,
                     gate=GateContext(documents))
    candidates, stats, complete = collect_candidates(ctx, methods)
    unique, merged_away = merge_duplicates(candidates)
    kept, cut = apply_limits(unique, per_document=limit_per_document(), per_object=limit_per_object())
    param = ensure_free_search_param(db, process, params)
    stored = store_candidates(db, process, param, kept, user_id=user_id)
    ran = [m for m in methods if m in complete]
    stale = reconcile_stale(db, process, param, stored["keys"], ran_methods=ran)
    summary = {
        "enabled": True, "methods": list(methods), "reconciled_methods": ran, "documents": len(documents),
        "candidates": len(candidates), "merged_duplicates": merged_away, "kept": len(kept), **cut,
        "by_method": {m: sum(1 for c in kept if c.method == m) for m in ALL_METHODS if any(c.method == m for c in kept)},
        "store": {k: v for k, v in stored.items() if k != "keys"}, "stale": stale, "detectors": stats,
        "tables": ctx.diagnostics.get("tables"), "seconds": round(time.monotonic() - t0, 1),
    }
    changed = sum(stored.get(key, 0) for key in ("created", "refreshed", "flagged")) + sum(stale.values())
    if changed:
        # an identical re-run changes nothing and therefore leaves no audit entry (the inspector's trail is of changes)
        db.add(AuditLog(user_id=user_id, action="FREE_HYPOTHESES_RUN", object_id=process.object_id, project_id=int(process.project_id),
                        process_id=str(process.id), details=_json_safe(summary), ip_address=get_client_ip()))
        db.flush()
    return summary


def _json_safe(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
