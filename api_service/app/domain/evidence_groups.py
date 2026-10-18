"""Incremental-safe upsert for EvidenceGroup rows.

The extraction/comparison logic in `official_evidence.py`, `official_rule_packs.py`
and the demo path in `v3_pipeline.py` recomputes *candidate* findings on every
run. What it must never do on its own is decide whether a finding an inspector
already looked at should be thrown away. That decision lives here, in one
place, so all three producers share the same rule:

- same evidence basis as before -> touch nothing (no write at all).
- different basis, no inspector decision recorded yet -> safe to refresh in
  place (fields, fragments, hash).
- different basis, but a decision exists -> the decision and its history are
  left exactly as they are; the row is flagged `needs_reverification` and the
  freshly computed values (and the fragments they came from) are parked under
  `delta.pending_reverification` so an inspector can see what changed without it
  being silently applied.
- the evidence later returns to exactly the basis the decision was made on -> the
  flag (and the parked values / `evidence_no_longer_found`) is cleared, so a stale
  pending value can never be folded over the value the evidence shows.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import AuditLog, EvidenceDecision, EvidenceGroup, InspectionProcess, Param
from ..request_context import get_client_ip
from .evidence_tier import finalize_delta


def _hash_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_basis_hash(*, expected_value: Any, actual_value: Any, delta: Any, fragment_specs: list[dict[str, Any]]) -> str:
    """Fingerprint of the raw evidence backing a finding: the comparison
    inputs/outputs plus the identity+value of every supporting fragment
    (which document version, which page, what was actually read there).
    Two runs that produce the same fingerprint found the exact same evidence,
    even if unrelated fields (confidence, review_priority) drifted."""
    fingerprint = {
        "expected_value": expected_value,
        "actual_value": actual_value,
        "delta": delta,
        "fragments": sorted(
            (
                {
                    "document_version_id": spec.get("document_version_id"),
                    "source_fragment_id": spec.get("source_fragment_id"),
                    "page": spec.get("page"),
                    "extracted_value": spec.get("extracted_value"),
                    "role": spec.get("role"),
                    "file_sha256": spec.get("file_sha256"),
                    "revision": spec.get("revision"),
                }
                for spec in fragment_specs
            ),
            key=lambda item: json.dumps(item, sort_keys=True, default=str),
        ),
    }
    return _hash_json(fingerprint)


def _json_safe_specs(fragment_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return json.loads(json.dumps(fragment_specs, ensure_ascii=False, sort_keys=True, default=str))


def _clear_stale_flag(group: EvidenceGroup) -> bool:
    """Drops `pending_reverification` / `evidence_no_longer_found` from a flagged row whose basis is back to the
    reviewed one. Returns True when anything changed. A row nobody flagged is left strictly untouched."""
    delta = group.delta if isinstance(group.delta, dict) else {}
    if not (group.needs_reverification or "pending_reverification" in delta or "evidence_no_longer_found" in delta):
        return False
    cleaned = {key: value for key, value in delta.items() if key not in ("pending_reverification", "evidence_no_longer_found")}
    group.delta = cleaned
    group.needs_reverification = False
    group.basis_changed_at = None
    return True


def _audit(db: Session, *, action: str, process: InspectionProcess, user_id: int | None, object_id: str | None, details: dict[str, Any]) -> None:
    db.add(AuditLog(
        user_id=user_id,
        action=action,
        object_id=object_id or process.object_id,
        project_id=int(process.project_id),
        process_id=str(process.id),
        details=details,
        ip_address=get_client_ip(),
    ))
    db.flush()


def upsert_evidence_group(
    db: Session,
    process: InspectionProcess,
    param: Param,
    group_key: str,
    *,
    fields: dict[str, Any],
    fragment_specs: list[dict[str, Any]],
    user_id: int | None = None,
) -> tuple[EvidenceGroup | None, bool]:
    """Create, refresh, or preserve-and-flag the EvidenceGroup identified by
    (process, param, group_key).

    Returns (group, should_write_fragments):
    - (None, False): evidence basis unchanged since the last run -- caller
      must not touch this group's fragments or anything else about it.
    - (group, True): a new or freshly-refreshed row; caller should (re)write
      this group's EvidenceFragment rows.
    - (group, False): an inspector decision already exists and the basis
      changed; the row is flagged for re-review, fragments are left as-is.
    """
    # One contract for every producer: evidence tier, no-result state and the explanation block (evidence_tier.py).
    if isinstance(fields.get("delta"), dict):
        fields = {**fields, "delta": finalize_delta(param, fields)}
    new_hash = compute_basis_hash(
        expected_value=fields.get("expected_value"),
        actual_value=fields.get("actual_value"),
        delta=fields.get("delta"),
        fragment_specs=fragment_specs,
    )
    existing = (
        db.query(EvidenceGroup)
        .filter(
            EvidenceGroup.process_id == process.id,
            EvidenceGroup.param_id == int(param.id),
            EvidenceGroup.group_key == group_key,
        )
        .first()
    )
    if existing is None:
        group = EvidenceGroup(
            process_id=str(process.id),
            project_id=int(process.project_id),
            organization_id=int(process.organization_id),
            object_id=process.object_id,
            param_id=int(param.id),
            group_key=group_key,
            evidence_basis_hash=new_hash,
            needs_reverification=False,
            **fields,
        )
        db.add(group)
        db.flush()
        return group, True

    if existing.evidence_basis_hash == new_hash:
        # The stored hash is the basis the row's values (and any inspector decision) were made on; a flagged row
        # never advances it. Finding exactly that basis again therefore means whatever flagged the row -- a changed
        # basis parked under `pending_reverification`, or `evidence_no_longer_found` -- no longer applies. Leaving
        # the flag would let the next decision fold a stale pending value over the value the evidence shows.
        if _clear_stale_flag(existing):
            db.add(existing)
            db.flush()
            return existing, False
        return None, False

    has_decision = (
        db.query(EvidenceDecision.id)
        .filter(EvidenceDecision.evidence_group_id == existing.id)
        .first()
        is not None
    )
    if has_decision:
        new_pending = {
            "expected_value": fields.get("expected_value"),
            "actual_value": fields.get("actual_value"),
            "delta": fields.get("delta"),
        }
        prior_pending = (existing.delta or {}).get("pending_reverification") if isinstance(existing.delta, dict) else None
        prior_hash = prior_pending.get("basis_hash") if isinstance(prior_pending, dict) else None
        already_flagged_for_this = (
            bool(existing.needs_reverification)
            and isinstance(prior_pending, dict)
            and (
                prior_hash == new_hash
                if prior_hash is not None
                else (
                    prior_pending.get("expected_value") == new_pending["expected_value"]
                    and prior_pending.get("actual_value") == new_pending["actual_value"]
                    and prior_pending.get("delta") == new_pending["delta"]
                )
            )
        )
        if already_flagged_for_this:
            # Same mismatch already on record (e.g. a full rerun re-derived the
            # identical new evidence again): nothing changed since the last
            # flag, so don't re-write the row or spam another audit entry.
            return existing, False
        merged_delta = dict(existing.delta or {})
        merged_delta["pending_reverification"] = {
            **new_pending,
            # What the inspector will actually be shown after re-deciding: the pending values are folded in together
            # with the fragments they were computed from (`_apply_pending_reverification`), so the card never pairs
            # new values with old citations and the next unchanged run recognises the basis as already reviewed.
            "basis_hash": new_hash,
            "fragments": _json_safe_specs(fragment_specs),
            "computed_at": datetime.utcnow().isoformat(),
        }
        existing.delta = merged_delta
        existing.needs_reverification = True
        existing.basis_changed_at = datetime.utcnow()
        db.add(existing)
        db.flush()
        _audit(
            db,
            action="EVIDENCE_BASIS_CHANGED",
            process=process,
            user_id=user_id,
            object_id=existing.object_id,
            details={
                "evidence_group_id": int(existing.id),
                "param_code": param.code,
                "group_key": group_key,
                "previous_hash": existing.evidence_basis_hash,
                "new_hash": new_hash,
                "decision_preserved": True,
                "reason": "evidence_basis_changed",
            },
        )
        return existing, False

    for key, value in fields.items():
        setattr(existing, key, value)
    existing.evidence_basis_hash = new_hash
    existing.needs_reverification = False
    existing.basis_changed_at = None
    for fragment in list(existing.fragments):
        db.delete(fragment)
    db.add(existing)
    db.flush()
    return existing, True


def sweep_orphaned_evidence_groups(
    db: Session,
    process: InspectionProcess,
    param_ids: set[int],
    touched_keys: dict[int, set[str]],
    *,
    user_id: int | None = None,
) -> None:
    """After a (possibly param-scoped) recompute, reconcile groups under
    `param_ids` whose key was not produced this run: preserve+flag if an
    inspector already decided on them, delete otherwise (nothing lost -- an
    undecided CANDIDATE/technical-status row carries no history)."""
    if not param_ids:
        return
    groups = (
        db.query(EvidenceGroup)
        .filter(EvidenceGroup.process_id == process.id, EvidenceGroup.param_id.in_(param_ids))
        .all()
    )
    for group in groups:
        keep_keys = touched_keys.get(int(group.param_id), set())
        if group.group_key in keep_keys:
            continue
        has_decision = (
            db.query(EvidenceDecision.id)
            .filter(EvidenceDecision.evidence_group_id == group.id)
            .first()
            is not None
        )
        if has_decision:
            merged_delta = dict(group.delta or {})
            merged_delta["evidence_no_longer_found"] = True
            group.delta = merged_delta
            group.needs_reverification = True
            group.basis_changed_at = datetime.utcnow()
            db.add(group)
            _audit(
                db,
                action="EVIDENCE_BASIS_CHANGED",
                process=process,
                user_id=user_id,
                object_id=group.object_id,
                details={
                    "evidence_group_id": int(group.id),
                    "group_key": group.group_key,
                    "reason": "evidence_no_longer_found",
                    "decision_preserved": True,
                },
            )
        else:
            db.delete(group)
    db.flush()
