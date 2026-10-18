"""Inspector dashboard (TZ module 7): objects / checks with a colour indication.

The colour is derived from the data at read time; no new server-side status of a violation is introduced and nothing
here is stored. The rule is fixed in `classify` (and shown in the UI as `RULE_TEXT`):

  GRAY    the check is not finished (queued, processing, failed) or no parameter has a comparison result (no data).
  RED     there is a violation confirmed by an inspector, or an unreviewed violation candidate of HIGH review priority.
  YELLOW  there are unreviewed candidates, suggestions or hypotheses, or findings that wait for a clarification.
  GREEN   everything found is reviewed and no violation is confirmed.

GRAY wins over the others: a running check says nothing about the object yet. RED wins over YELLOW.
The inspector's own decisions are the only way out of YELLOW/RED, so the colour of a finished check changes with their work.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import ConstructionObject, EvidenceGroup, InspectionProcess, Param, Project, Protocol, Suspicion
from . import evidence_tier as et

RED, YELLOW, GREEN, GRAY = "RED", "YELLOW", "GREEN", "GRAY"
COLORS = (RED, YELLOW, GREEN, GRAY)
COLOR_TITLES = {RED: "Красный", YELLOW: "Жёлтый", GREEN: "Зелёный", GRAY: "Серый"}
FINISHED_PROCESS_STATUSES = {"READY", "VERIFYING", "COMPLETED", "FINALIZED"}
RULE_TEXT = {
    RED: "Красный: есть нарушение, подтверждённое инспектором, или нерассмотренный кандидат в нарушения высокого приоритета.",
    YELLOW: "Жёлтый: есть нерассмотренные кандидаты, предположения или гипотезы либо находки, ожидающие уточнения.",
    GREEN: "Зелёный: всё найденное рассмотрено инспектором, подтверждённых нарушений нет.",
    GRAY: "Серый: проверка не завершена или данных нет (ни один параметр не получил результата сравнения).",
}
REASON_TEXT = {
    "process_not_finished": "проверка не завершена",
    "no_data": "нет данных: ни одного результата сравнения",
    "confirmed_violation": "есть подтверждённое нарушение",
    "high_priority_candidate": "есть нерассмотренный кандидат высокого приоритета",
    "unreviewed_candidates": "есть нерассмотренные кандидаты или предположения",
    "unreviewed_hypotheses": "есть нерассмотренные гипотезы",
    "clarification_requested": "есть находки, ожидающие уточнения",
    "all_reviewed": "всё найденное рассмотрено, нарушений нет",
}
# the finding statuses that carry information for the dashboard; the rest is only counted
_RELEVANT = ("CANDIDATE", "CONFIRMED_VIOLATION", "SUSPICION", "CLARIFICATION_REQUIRED")


def classify(process_status: str | None, counts: dict[str, int]) -> tuple[str, str]:
    """(colour, reason code). `counts` keys: total, confirmed, high_priority_candidates, candidates, hypotheses, clarification."""
    if str(process_status or "").upper() not in FINISHED_PROCESS_STATUSES:
        return GRAY, "process_not_finished"
    if not counts.get("total"):
        return GRAY, "no_data"
    if counts.get("confirmed"):
        return RED, "confirmed_violation"
    if counts.get("high_priority_candidates"):
        return RED, "high_priority_candidate"
    if counts.get("candidates"):
        return YELLOW, "unreviewed_candidates"
    if counts.get("hypotheses"):
        return YELLOW, "unreviewed_hypotheses"
    if counts.get("clarification"):
        return YELLOW, "clarification_requested"
    return GREEN, "all_reviewed"


def _day_start(value: date | datetime | str | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return datetime.combine(date.fromisoformat(str(value)[:10]), time.min)


def _counts(rows: list[Any]) -> dict[str, int]:
    """`rows`: (finding_status, review_priority, delta, suspicion_status) of one check (after the section filter)."""
    counts: Counter = Counter()
    for status, priority, delta, hypothesis_status in rows:
        delta = delta if isinstance(delta, dict) else {}
        is_hypothesis = status == "SUSPICION" or delta.get("matrix_scope") == "FREE_SEARCH"
        if status == "CONFIRMED_VIOLATION":
            counts["confirmed"] += 1
        elif status == "CLARIFICATION_REQUIRED":
            counts["clarification"] += 1
        elif is_hypothesis and status != "CANDIDATE":
            if str(hypothesis_status or "PENDING") == "PENDING":
                counts["hypotheses"] += 1
        elif status == "CANDIDATE":
            counts["candidates"] += 1
            tier = et.tier_of(status, delta)
            if tier == et.TIER_VIOLATION_CANDIDATE:
                counts["violation_candidates"] += 1
                if str(priority or "").upper() == "HIGH":
                    counts["high_priority_candidates"] += 1
            else:
                counts["suggestions"] += 1
    return dict(counts)


def dashboard(
    db: Session,
    *,
    organization_id: int,
    project_id: int | None = None,
    section: str | None = None,
    colors: set[str] | None = None,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """One row per check (inspection process) of the organisation, newest first. `section` restricts the colour and the
    counts to the parameters of that Matrix section (and hides checks without findings in it); `colors` keeps the
    chosen colours; `date_from`/`date_to` filter by the date the check was created (inclusive)."""
    query = db.query(InspectionProcess).filter(InspectionProcess.organization_id == int(organization_id))
    if project_id:
        query = query.filter(InspectionProcess.project_id == int(project_id))
    start, end = _day_start(date_from), _day_start(date_to)
    if start:
        query = query.filter(InspectionProcess.created_at >= start)
    if end:
        query = query.filter(InspectionProcess.created_at < end + timedelta(days=1))
    processes = query.order_by(InspectionProcess.created_at.desc(), InspectionProcess.id.desc()).all()
    ids = [str(p.id) for p in processes]

    by_process: dict[str, list[Any]] = defaultdict(list)
    sections: set[str] = set()
    if ids:
        rows = (
            db.query(EvidenceGroup.process_id, EvidenceGroup.finding_status, EvidenceGroup.review_priority, EvidenceGroup.delta,
                     Param.section, Suspicion.inspector_status)
            .outerjoin(Param, Param.id == EvidenceGroup.param_id)
            .outerjoin(Suspicion, Suspicion.evidence_group_id == EvidenceGroup.id)
            .filter(EvidenceGroup.process_id.in_(ids), EvidenceGroup.finding_status.in_(_RELEVANT))
            .all()
        )
        for process_id, status, priority, delta, param_section, hypothesis_status in rows:
            if section and str(param_section or "") != section:
                continue
            by_process[str(process_id)].append((status, priority, delta, hypothesis_status))
    # groups that carry a result of a comparison (anything but "no evidence / not comparable / not applicable")
    with_result: Counter = Counter()
    section_present: set[str] = set()
    if ids:
        from sqlalchemy import func

        for process_id, param_section, number in (
            db.query(EvidenceGroup.process_id, Param.section, func.count(EvidenceGroup.id))
            .outerjoin(Param, Param.id == EvidenceGroup.param_id)
            .filter(EvidenceGroup.process_id.in_(ids), EvidenceGroup.finding_status.in_(_RELEVANT + ("NEGATIVE_VERIFIED",)))
            .group_by(EvidenceGroup.process_id, Param.section).all()
        ):
            if param_section:
                sections.add(str(param_section))
            if section and str(param_section or "") != section:
                continue
            with_result[str(process_id)] += int(number)
            section_present.add(str(process_id))

    project_names = {int(pid): name for pid, name in db.query(Project.id, Project.name).filter(Project.organization_id == int(organization_id)).all()}
    object_names = {str(oid): name for oid, name in db.query(ConstructionObject.id, ConstructionObject.name)
                    .filter(ConstructionObject.organization_id == int(organization_id)).all()}
    latest_protocol: dict[str, tuple[int, int, str]] = {}
    if ids:
        for pid, version, protocol_id, status in (db.query(Protocol.process_id, Protocol.version, Protocol.id, Protocol.status)
                                                  .filter(Protocol.process_id.in_(ids)).order_by(Protocol.version.asc()).all()):
            latest_protocol[str(pid)] = (int(protocol_id), int(version), str(status))

    items = []
    for process in processes:
        pid = str(process.id)
        if section and pid not in section_present:
            continue
        counts = _counts(by_process.get(pid, []))
        counts["total"] = int(with_result.get(pid, 0))
        color, reason = classify(process.status, counts)
        if colors and color not in colors:
            continue
        protocol = latest_protocol.get(pid)
        object_name = next((object_names[key] for key in (str(process.object_id), f"{process.object_id}") if key in object_names), None)
        items.append({
            "process_id": pid, "project_id": int(process.project_id), "project_name": project_names.get(int(process.project_id)),
            "object_id": process.object_id, "object_name": object_name or process.object_id,
            "process_status": process.status, "color": color, "color_title": COLOR_TITLES[color], "reason": reason,
            "reason_text": REASON_TEXT[reason],
            "counts": {key: int(counts.get(key, 0)) for key in ("confirmed", "high_priority_candidates", "violation_candidates",
                                                                  "suggestions", "hypotheses", "clarification")},
            "results_total": int(with_result.get(pid, 0)),
            "created_at": process.created_at, "updated_at": process.updated_at, "finalized_at": process.finalized_at,
            "protocol": {"id": protocol[0], "version": protocol[1], "status": protocol[2]} if protocol else None,
        })
    summary = Counter(item["color"] for item in items[:])
    return {
        "items": items[: int(limit)], "total": len(items),
        "summary": {color: int(summary.get(color, 0)) for color in COLORS},
        "rules": RULE_TEXT, "colors": list(COLORS),
        "filters": {"sections": sorted(sections), "section": section, "colors": sorted(colors or []),
                    "date_from": str(date_from) if date_from else None, "date_to": str(date_to) if date_to else None,
                    "project_id": project_id, "date_field": "created_at"},
    }
