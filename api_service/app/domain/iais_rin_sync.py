"""ТЗ 9.6: outbound sync of a finalized protocol to the external ИАИС "РиН"
system. Marked Low priority by the ТЗ itself.

Scoping notes:

- The internal FINALIZED status (`InspectionProcess.status` /
  `Protocol.status`) is the load-bearing terminal state this codebase already
  uses everywhere (it blocks further uploads/decisions/edits -- see
  `v3_pipeline.finalize_protocol`). This module does not change that
  semantics on a РИН sync failure. ТЗ 9.6's "протокол не считается
  финализированным до успешной отправки" is honored at the level of a
  *separate* `iais_rin_sync` block inside `Protocol.payload_json`
  (statuses below), not by reverting the FINALIZED gate itself -- doing that
  would ripple into every other endpoint that already treats FINALIZED as
  terminal, for a Low-priority integration with no real target system.
- Retry scheduling (1/5/15 min, then hourly -- ТЗ 9.6) is expressed as a
  `next_retry_at` timestamp inside that same block rather than a background
  timer/queue: `sync_protocol_to_iais_rin` makes one attempt synchronously
  (called from the finalize endpoint via BackgroundTasks);
  `retry_pending_iais_rin_syncs` is a pure, timestamp-driven sweep a caller
  can invoke on any cadence. Tests call both directly with an injected `now`,
  so retry behavior is fully deterministic without sleeping for real minutes.
- "Блокировка автоматической дозагрузки при финализированном протоколе"
  (ТЗ 9.6): this codebase has no inbound receiver for ИАИС РИН pushing
  documents at all (it is upload/pull driven throughout), so there is no
  auto-fetch path to block. The closest existing behavior --
  `get_or_create_open_process` never reopens a FINALIZED process, it starts a
  fresh one -- already gives the "don't touch the finalized protocol, start a
  new inspection instead" outcome ТЗ 9.6 asks for, for the one upload path
  that actually exists.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..clients.iais_rin_client import IaisRinClient, IaisRinError, IaisRinRejectedError
from ..config import settings
from ..db.models import MonitoringMetric, Protocol

IAIS_SYNC_NOT_CONFIGURED = "NOT_CONFIGURED"
IAIS_SYNC_SYNCED = "SYNCED"
IAIS_SYNC_PENDING_SYNC = "PENDING_SYNC"
IAIS_SYNC_REJECTED = "REJECTED"

MONITORING_SERVICE_NAME = "iais_rin_sync"


def build_inspection_payload(protocol: Protocol) -> dict[str, Any]:
    return {
        "process_id": protocol.process_id,
        "protocol_id": int(protocol.id),
        "version": protocol.version,
        "object_id": protocol.object_id,
        "finalized_at": protocol.finalized_at.isoformat() if protocol.finalized_at else None,
        "matrix_version": protocol.matrix_version,
        "payload": protocol.payload_json or {},
    }


def _next_retry_delay_seconds(attempts: int) -> int:
    delays = settings.IAIS_RIN_RETRY_DELAYS_SECONDS
    if attempts <= len(delays):
        return delays[attempts - 1]
    return settings.IAIS_RIN_PENDING_SYNC_RETRY_SECONDS


def record_monitoring_metric(
    db: Session, metric_name: str, value: float, *, service_name: str = MONITORING_SERVICE_NAME, tags: dict[str, Any] | None = None
) -> MonitoringMetric:
    row = MonitoringMetric(metric_name=metric_name, value=float(value), service_name=service_name, tags=tags or {})
    db.add(row)
    db.flush()
    return row


def sync_state_of(protocol: Protocol) -> dict[str, Any]:
    return dict((protocol.payload_json or {}).get("iais_rin_sync") or {})


async def sync_protocol_to_iais_rin(
    db: Session,
    protocol: Protocol,
    *,
    client: IaisRinClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attempt one send of `protocol` to ИАИС РИН.

    Raises HTTPException(409) if the protocol is not PROTOCOL_FINALIZED (ТЗ
    9.6: "передача возможна только при статусе протокола PROTOCOL_FINALIZED").
    Otherwise never raises: a retryable failure or an outright rejection is
    recorded in `Protocol.payload_json["iais_rin_sync"]` and returned, not
    thrown -- this is routinely invoked from a background task with nothing
    to catch it.
    """
    if str(protocol.status) != "FINALIZED":
        raise HTTPException(status_code=409, detail="Protocol must be PROTOCOL_FINALIZED before syncing to ИАИС РИН")

    now = now or datetime.utcnow()
    client = client or IaisRinClient()
    payload = build_inspection_payload(protocol)
    sync_state = sync_state_of(protocol)
    attempts = int(sync_state.get("attempts", 0)) + 1
    tags = {"process_id": protocol.process_id, "protocol_id": int(protocol.id)}

    if not client.configured:
        sync_state.update({
            "status": IAIS_SYNC_NOT_CONFIGURED,
            "attempts": attempts,
            "last_attempt_at": now.isoformat(),
            "last_error": "IAIS_RIN_BASE_URL is not configured",
            "next_retry_at": None,
        })
        record_monitoring_metric(db, "iais_rin_sync_skipped_total", 1.0, tags=tags)
        protocol.payload_json = {**(protocol.payload_json or {}), "iais_rin_sync": sync_state}
        db.add(protocol)
        db.commit()
        db.refresh(protocol)
        return sync_state

    started = time.monotonic()
    outcome = "success"
    try:
        response = await client.send_inspection_result(process_id=str(protocol.process_id), payload=payload)
        sync_state.update({
            "status": IAIS_SYNC_SYNCED,
            "attempts": attempts,
            "last_attempt_at": now.isoformat(),
            "last_error": None,
            "next_retry_at": None,
            "synced_at": now.isoformat(),
            "response": response,
        })
    except IaisRinRejectedError as exc:
        outcome = "rejected"
        sync_state.update({
            "status": IAIS_SYNC_REJECTED,
            "attempts": attempts,
            "last_attempt_at": now.isoformat(),
            "last_error": str(exc),
            "next_retry_at": None,
        })
    except IaisRinError as exc:
        outcome = "retry"
        delay_seconds = _next_retry_delay_seconds(attempts)
        sync_state.update({
            "status": IAIS_SYNC_PENDING_SYNC,
            "attempts": attempts,
            "last_attempt_at": now.isoformat(),
            "last_error": str(exc),
            "next_retry_at": (now + timedelta(seconds=delay_seconds)).isoformat(),
        })

    duration = time.monotonic() - started
    record_monitoring_metric(db, "iais_rin_sync_duration_seconds", duration, tags={**tags, "outcome": outcome})
    record_monitoring_metric(db, "iais_rin_sync_attempts_total", float(attempts), tags={**tags, "outcome": outcome})
    protocol.payload_json = {**(protocol.payload_json or {}), "iais_rin_sync": sync_state}
    db.add(protocol)
    db.commit()
    db.refresh(protocol)
    return sync_state


async def retry_pending_iais_rin_syncs(
    db: Session, *, now: datetime | None = None, client: IaisRinClient | None = None
) -> list[dict[str, Any]]:
    """Sweep every FINALIZED protocol whose last sync attempt left it
    PENDING_SYNC and whose `next_retry_at` has passed, and retry it once.
    Pure and timestamp-driven -- safe to call from a cron/worker loop in
    production, or directly (with an injected `now`) from a test."""
    now = now or datetime.utcnow()
    candidates = db.query(Protocol).filter(Protocol.status == "FINALIZED").all()
    results: list[dict[str, Any]] = []
    for protocol in candidates:
        state = sync_state_of(protocol)
        if state.get("status") != IAIS_SYNC_PENDING_SYNC:
            continue
        next_retry_raw = state.get("next_retry_at")
        if not next_retry_raw:
            continue
        try:
            next_retry_at = datetime.fromisoformat(str(next_retry_raw))
        except ValueError:
            continue
        if next_retry_at > now:
            continue
        result = await sync_protocol_to_iais_rin(db, protocol, client=client, now=now)
        results.append({"protocol_id": int(protocol.id), "process_id": protocol.process_id, **result})
    return results
