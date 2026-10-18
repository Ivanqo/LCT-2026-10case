"""Weekly report for ML engineers (TZ module 10): what the inspectors did with the system's output in the last seven days.

Built only from what the application stored: inspector decisions on matrix findings (`evidence_decisions`), reviews of
free hypotheses (`inspector_edits`, entity HYPOTHESIS_REVIEW), the rejection log and `ml_retraining_logs`. Nothing is
estimated: a number is a count of decisions, "precision by inspectors' decisions" is confirmed / (confirmed + rejected)
with a Wilson interval, clarification requests are shown apart. The report says "данных мало" instead of drawing
conclusions from a handful of decisions; recommendations come from the fixed rules in `RECOMMENDATION_RULES` and cite
the counts they stand on.

The window is seven days ending on `end` (inclusive, UTC dates); the previous window is the seven days before it.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from html import escape
import math
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import EvidenceDecision, EvidenceGroup, InspectorEdit, MLRetrainingLog, Param, RejectionLog

SCHEMA_VERSION = "case10-ml-weekly-report-v1"
MIN_DECISIONS_FOR_REPORT = 20     # below this the whole report is flagged "данных мало" and gives no recommendations
MIN_BUCKET_FOR_RULES = 10         # a method / section / reason needs this many decisions before a rule may fire
REJECT_SHARE_ALERT = 0.60         # "метод X отклонён > 60 %"
CLARIFY_SHARE_ALERT = 0.30
TOP_REASON_SHARE = 0.40
PRECISION_DROP_ALERT = 0.15
HYPOTHESIS_SECTION = "Свободные гипотезы (вне Матрицы)"
OUTCOMES = ("confirmed", "rejected", "clarified")
_DECISION_OUTCOME = {"Confirm": "confirmed", "Reject": "rejected", "Clarification Required": "clarified"}
_HYPOTHESIS_OUTCOME = {"CONFIRMED": "confirmed", "REJECTED": "rejected", "CLARIFICATION_REQUIRED": "clarified"}
REASON_TITLES = {
    "NOT_A_VIOLATION": "несоответствие не подтверждено фактически",
    "STALE_REVISION": "сравнение с неактуальной редакцией",
    "WRONG_LOCALIZATION": "доказательство привязано не к тому месту/листу",
    "DATA_ERROR": "ошибка распознавания исходных данных",
    "OTHER": "другая причина (в комментарии)",
}
REASON_ADVICE = {
    "NOT_A_VIOLATION": "пересмотреть условия триггеров и пороги: значения сравниваются верно, но расхождение не является нарушением",
    "STALE_REVISION": "проверить выбор актуальной редакции и порядок редакций раздела",
    "WRONG_LOCALIZATION": "проверить привязку значения к месту/листу и сопоставление помещений и систем",
    "DATA_ERROR": "проверить качество распознавания и извлечения значений (OCR, разбор таблиц) на листах с отклонёнными находками",
    "OTHER": "разобрать комментарии к отклонениям вручную: единой причины не названо",
}
RECOMMENDATION_RULES = {
    "R1_METHOD_REJECTED": f"метод отклонён в более чем {int(REJECT_SHARE_ALERT * 100)} % решений (не менее {MIN_BUCKET_FOR_RULES} решений): пересмотреть порог или правило метода",
    "R2_SECTION_REJECTED": f"раздел Матрицы отклонён в более чем {int(REJECT_SHARE_ALERT * 100)} % решений (не менее {MIN_BUCKET_FOR_RULES}): пересмотреть правила раздела",
    "R3_TOP_REASON": f"одна причина составляет не менее {int(TOP_REASON_SHARE * 100)} % отклонений (не менее {MIN_BUCKET_FOR_RULES} отклонений): целевая донастройка по причине",
    "R4_PRECISION_DROP": f"доля подтверждённых упала на {int(PRECISION_DROP_ALERT * 100)} п. п. и более к прошлой неделе (не менее {MIN_BUCKET_FOR_RULES} решений в обе недели)",
    "R5_MANY_CLARIFICATIONS": f"уточнения составляют более {int(CLARIFY_SHARE_ALERT * 100)} % решений (не менее {MIN_BUCKET_FOR_RULES}): данных или привязки не хватает",
    "R6_RETRAINING_BLOCKED": "последний релиз переобучения заблокирован шлюзом приёмки",
}


# ------------------------------------------------------------------------------------------ statistics


def wilson(successes: int, trials: int, z: float = 1.96) -> list[float] | None:
    if trials <= 0:
        return None
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _bucket(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(event["outcome"] for event in events)
    confirmed, rejected, clarified = counts["confirmed"], counts["rejected"], counts["clarified"]
    judged = confirmed + rejected
    return {
        "decisions": len(events), "confirmed": confirmed, "rejected": rejected, "clarified": clarified,
        "precision_by_inspectors": _rate(confirmed, judged), "precision_wilson95": wilson(confirmed, judged), "judged": judged,
        "reject_share": _rate(rejected, len(events)), "clarify_share": _rate(clarified, len(events)),
    }


def _change(now: float | None, before: float | None) -> float | None:
    return round(now - before, 4) if now is not None and before is not None else None


# ------------------------------------------------------------------------------------------ data


def _window(end: date | str | None) -> tuple[datetime, datetime, datetime]:
    last = end if isinstance(end, date) and not isinstance(end, datetime) else (date.fromisoformat(str(end)[:10]) if end else datetime.utcnow().date())
    stop = datetime.combine(last + timedelta(days=1), time.min)
    return stop - timedelta(days=14), stop - timedelta(days=7), stop


def _events(db: Session, *, organization_id: int | None, project_id: int | None, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Every decision / hypothesis review in [since, until), oldest first, with the finding's section and method."""
    events: list[dict[str, Any]] = []
    query = (db.query(EvidenceDecision, EvidenceGroup, Param.section)
             .join(EvidenceGroup, EvidenceGroup.id == EvidenceDecision.evidence_group_id)
             .outerjoin(Param, Param.id == EvidenceGroup.param_id)
             .filter(EvidenceDecision.created_at >= since, EvidenceDecision.created_at < until))
    if organization_id:
        query = query.filter(EvidenceGroup.organization_id == int(organization_id))
    if project_id:
        query = query.filter(EvidenceGroup.project_id == int(project_id))
    for decision, group, section in query.order_by(EvidenceDecision.id.asc()).all():
        outcome = _DECISION_OUTCOME.get(str(decision.decision))
        if not outcome:
            continue
        delta = group.delta if isinstance(group.delta, dict) else {}
        events.append({"at": decision.created_at, "outcome": outcome, "reason_code": decision.reason_code if outcome == "rejected" else None,
                       "section": section or "Раздел не указан", "method": str(delta.get("discovery_method") or delta.get("source") or "unknown"),
                       "kind": "finding"})
    hypothesis = (db.query(InspectorEdit, EvidenceGroup)
                  .outerjoin(EvidenceGroup, EvidenceGroup.id == InspectorEdit.evidence_group_id)
                  .filter(InspectorEdit.entity_type == "HYPOTHESIS_REVIEW", InspectorEdit.created_at >= since, InspectorEdit.created_at < until))
    if organization_id:
        hypothesis = hypothesis.filter(InspectorEdit.organization_id == int(organization_id))
    if project_id:
        hypothesis = hypothesis.filter(InspectorEdit.project_id == int(project_id))
    for edit, group in hypothesis.order_by(InspectorEdit.id.asc()).all():
        outcome = _HYPOTHESIS_OUTCOME.get(str(edit.action))
        if not outcome:
            continue
        delta = (group.delta if group is not None and isinstance(group.delta, dict) else {})
        events.append({"at": edit.created_at, "outcome": outcome, "reason_code": None, "section": HYPOTHESIS_SECTION,
                       "method": str(delta.get("discovery_method") or delta.get("source") or "unknown"), "kind": "hypothesis"})
    return events


def _grouped(events: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(event[key])].append(event)
    return grouped


def _breakdown(current: list[dict[str, Any]], previous: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    now, before = _grouped(current, key), _grouped(previous, key)
    rows = []
    for name in sorted(set(now) | set(before), key=lambda item: (-len(now.get(item, [])), item)):
        this, last = _bucket(now.get(name, [])), _bucket(before.get(name, []))
        rows.append({"name": name, **this, "previous_week": {k: last[k] for k in ("decisions", "confirmed", "rejected", "clarified", "precision_by_inspectors")},
                     "decisions_change": this["decisions"] - last["decisions"],
                     "precision_change": _change(this["precision_by_inspectors"], last["precision_by_inspectors"])})
    return rows


def _reasons(current: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = Counter(event["reason_code"] or "OTHER" for event in current if event["outcome"] == "rejected" and event["kind"] == "finding")
    before = Counter(event["reason_code"] or "OTHER" for event in previous if event["outcome"] == "rejected" and event["kind"] == "finding")
    total = sum(now.values())
    return [{"reason_code": code, "title": REASON_TITLES.get(code, code), "count": count, "share": _rate(count, total),
             "previous_week": before.get(code, 0), "change": count - before.get(code, 0)}
            for code, count in sorted(now.items(), key=lambda item: (-item[1], item[0]))] + [
        {"reason_code": code, "title": REASON_TITLES.get(code, code), "count": 0, "share": 0.0, "previous_week": count, "change": -count}
        for code, count in sorted(before.items()) if code not in now]


# ------------------------------------------------------------------------------------------ recommendations


def recommendations(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Fixed rules over the report's own numbers. Each item names the rule, the target and the counts it rests on."""
    if not report["data_sufficiency"]["enough"]:
        return []
    out: list[dict[str, Any]] = []

    def add(rule: str, severity: str, target: str, text: str, **evidence: Any) -> None:
        out.append({"rule": rule, "severity": severity, "target": target, "text": text, "evidence": evidence})

    for row in report["by_method"]:
        if row["decisions"] >= MIN_BUCKET_FOR_RULES and (row["reject_share"] or 0) > REJECT_SHARE_ALERT:
            add("R1_METHOD_REJECTED", "high", row["name"],
                f"Метод «{row['name']}» отклонён в {round(100 * row['reject_share'])} % решений ({row['rejected']} из {row['decisions']}) — пересмотреть порог или правило метода.",
                rejected=row["rejected"], decisions=row["decisions"])
    for row in report["by_section"]:
        if row["decisions"] >= MIN_BUCKET_FOR_RULES and (row["reject_share"] or 0) > REJECT_SHARE_ALERT:
            add("R2_SECTION_REJECTED", "high", row["name"],
                f"Раздел «{row['name']}»: отклонено {round(100 * row['reject_share'])} % решений ({row['rejected']} из {row['decisions']}) — пересмотреть правила раздела.",
                rejected=row["rejected"], decisions=row["decisions"])
    rejected_findings = sum(item["count"] for item in report["top_reject_reasons"])
    if rejected_findings >= MIN_BUCKET_FOR_RULES and report["top_reject_reasons"]:
        top = report["top_reject_reasons"][0]
        if (top["share"] or 0) >= TOP_REASON_SHARE:
            add("R3_TOP_REASON", "medium", top["reason_code"],
                f"Главная причина отклонений — «{top['title']}» ({top['count']} из {rejected_findings}): {REASON_ADVICE.get(top['reason_code'], 'разобрать вручную')}.",
                count=top["count"], rejected=rejected_findings)
    this, last = report["totals"], report["previous_week_totals"]
    if (this["judged"] >= MIN_BUCKET_FOR_RULES and last["judged"] >= MIN_BUCKET_FOR_RULES
            and (change := _change(this["precision_by_inspectors"], last["precision_by_inspectors"])) is not None and change <= -PRECISION_DROP_ALERT):
        add("R4_PRECISION_DROP", "high", "all",
            f"Доля подтверждённых по решениям инспекторов упала с {round(100 * last['precision_by_inspectors'])} % до {round(100 * this['precision_by_inspectors'])} % — проверить, что изменилось в данных или правилах за неделю.",
            previous=last["precision_by_inspectors"], current=this["precision_by_inspectors"])
    if this["decisions"] >= MIN_BUCKET_FOR_RULES and (this["clarify_share"] or 0) > CLARIFY_SHARE_ALERT:
        add("R5_MANY_CLARIFICATIONS", "medium", "all",
            f"Запросов уточнения {round(100 * this['clarify_share'])} % решений ({this['clarified']} из {this['decisions']}): проверить полноту комплектов и привязку находок.",
            clarified=this["clarified"], decisions=this["decisions"])
    log = report["retraining"]["latest"]
    if log and str(log.get("gate_status")) == "BLOCKED":
        add("R6_RETRAINING_BLOCKED", "low", str(log.get("candidate_model_version") or log.get("dataset_version")),
            "Последний релиз переобучения заблокирован шлюзом приёмки: " + ("; ".join(str(x) for x in (log.get("gate_reasons") or [])[:3]) or "причина не записана") + ".",
            log_id=log.get("id"))
    return out


# ------------------------------------------------------------------------------------------ report


def build_weekly_report(db: Session, *, organization_id: int | None = None, project_id: int | None = None, end: date | str | None = None) -> dict[str, Any]:
    previous_start, current_start, stop = _window(end)
    events = _events(db, organization_id=organization_id, project_id=project_id, since=previous_start, until=stop)
    current = [event for event in events if event["at"] >= current_start]
    previous = [event for event in events if event["at"] < current_start]
    totals, previous_totals = _bucket(current), _bucket(previous)

    logs = db.query(MLRetrainingLog)
    if organization_id:
        logs = logs.filter(MLRetrainingLog.organization_id == int(organization_id))
    if project_id:
        logs = logs.filter(MLRetrainingLog.project_id == int(project_id))
    log_rows = logs.order_by(MLRetrainingLog.created_at.desc(), MLRetrainingLog.id.desc()).limit(5).all()
    rejections = db.query(RejectionLog)
    if organization_id:
        rejections = rejections.filter(RejectionLog.organization_id == int(organization_id))
    if project_id:
        rejections = rejections.filter(RejectionLog.project_id == int(project_id))
    retraining_status = Counter(str(row.retraining_status) for row in rejections.all())

    def log_dict(row: MLRetrainingLog) -> dict[str, Any]:
        return {"id": int(row.id), "created_at": row.created_at.isoformat() if row.created_at else None, "dataset_version": row.dataset_version,
                "model_version": row.model_version, "candidate_model_version": row.candidate_model_version, "gate_status": row.gate_status,
                "approval_status": row.approval_status, "gate_reasons": list(row.gate_reasons or []), "metrics": row.metrics_json or {},
                "rollback_to_model_version": row.rollback_to_model_version}

    enough = totals["decisions"] >= MIN_DECISIONS_FOR_REPORT
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "period": {"from": current_start.date().isoformat(), "to": (stop - timedelta(days=1)).date().isoformat(),
                   "previous_from": previous_start.date().isoformat(), "previous_to": (current_start - timedelta(days=1)).date().isoformat()},
        "scope": {"organization_id": organization_id, "project_id": project_id},
        "data_sufficiency": {
            "enough": enough, "decisions": totals["decisions"], "min_decisions": MIN_DECISIONS_FOR_REPORT,
            "note": None if enough else (
                f"Данных мало: за неделю {totals['decisions']} решений инспекторов (для выводов нужно не менее {MIN_DECISIONS_FOR_REPORT}). "
                "Числа ниже верны, но рекомендации по донастройке не формируются; интервалы показывают, насколько они ненадёжны."),
        },
        "totals": totals,
        "previous_week_totals": previous_totals,
        "change": {"decisions": totals["decisions"] - previous_totals["decisions"],
                   "precision_by_inspectors": _change(totals["precision_by_inspectors"], previous_totals["precision_by_inspectors"]),
                   "rejected": totals["rejected"] - previous_totals["rejected"]},
        "by_section": _breakdown(current, previous, "section"),
        "by_method": _breakdown(current, previous, "method"),
        "top_reject_reasons": _reasons(current, previous),
        "hypotheses": _bucket([event for event in current if event["kind"] == "hypothesis"]),
        "retraining": {"latest": log_dict(log_rows[0]) if log_rows else None, "recent": [log_dict(row) for row in log_rows],
                       "rejection_log_by_retraining_status": dict(sorted(retraining_status.items())),
                       "note": None if log_rows else "Релизов переобучения в журнале ещё нет."},
        "definitions": {
            "decision": "каждое решение инспектора по находке (Подтвердить / Отклонить / Запрос уточнения) и каждая проверка свободной гипотезы за окно",
            "precision_by_inspectors": "подтверждённые / (подтверждённые + отклонённые); запросы уточнения не входят; это точность выдачи системы по оценке инспекторов, а не по эталону",
            "method": "источник находки (delta.source) или метод поиска гипотезы (discovery_method)",
        },
        "rules": RECOMMENDATION_RULES,
    }
    report["recommendations"] = recommendations(report)
    return report


# ------------------------------------------------------------------------------------------ HTML


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{round(100 * value)} %"


def _signed(value: float | int | None, *, percent: bool = False) -> str:
    if value is None:
        return "—"
    text = f"{round(100 * value):+d} п. п." if percent else f"{value:+d}"
    return text


def render_html(report: dict[str, Any]) -> str:
    def table(headers: list[str], rows: list[list[Any]]) -> str:
        if not rows:
            return "<p>Нет данных.</p>"
        return ("<table><tr>" + "".join(f"<th>{escape(h)}</th>" for h in headers) + "</tr>"
                + "".join("<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>" for row in rows) + "</table>")

    totals, change, period = report["totals"], report["change"], report["period"]

    def bucket_rows(items: list[dict[str, Any]]) -> list[list[Any]]:
        return [[r["name"], r["decisions"], r["confirmed"], r["rejected"], r["clarified"], _pct(r["precision_by_inspectors"]),
                 _signed(r["decisions_change"]), _signed(r["precision_change"], percent=True)] for r in items]

    head = ["", "Решений", "Подтв.", "Отклонено", "Уточнение", "Доля подтверждённых", "Δ решений", "Δ доли"]
    parts = [
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>Еженедельный отчёт ML</title>"
        "<style>body{font:14px/1.45 sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#222}table{border-collapse:collapse;width:100%;margin:8px 0}"
        "td,th{border:1px solid #bbb;padding:4px 8px;text-align:left}th{background:#eee}.notice{background:#fff4d6;border:1px solid #e0b84f;padding:8px 12px}"
        "</style></head><body>",
        f"<h1>Еженедельный отчёт для ML-инженеров</h1><p>Период: {escape(period['from'])} — {escape(period['to'])} (прошлая неделя: {escape(period['previous_from'])} — {escape(period['previous_to'])}).</p>",
    ]
    if report["data_sufficiency"]["note"]:
        parts.append(f"<p class='notice'>{escape(report['data_sufficiency']['note'])}</p>")
    low, high = (totals["precision_wilson95"] or [None, None])
    parts += [
        "<h2>Итоги недели</h2>",
        table(["Решений", "Подтверждено", "Отклонено", "Уточнение", "Доля подтверждённых (95 % интервал Уилсона)", "К прошлой неделе"],
              [[totals["decisions"], totals["confirmed"], totals["rejected"], totals["clarified"],
                f"{_pct(totals['precision_by_inspectors'])} ({_pct(low)}–{_pct(high)}), n={totals['judged']}",
                f"решений {_signed(change['decisions'])}, доля {_signed(change['precision_by_inspectors'], percent=True)}"]]),
        "<h2>По разделам Матрицы</h2>", table(["Раздел"] + head[1:], bucket_rows(report["by_section"])),
        "<h2>По методам</h2>", table(["Метод"] + head[1:], bucket_rows(report["by_method"])),
        "<h2>Причины отклонения</h2>",
        table(["Причина", "Отклонений", "Доля", "Прошлая неделя", "Δ"],
              [[f"{r['reason_code']} — {r['title']}", r["count"], _pct(r["share"]), r["previous_week"], _signed(r["change"])] for r in report["top_reject_reasons"]]),
        "<h2>Рекомендации по донастройке</h2>",
    ]
    if report["recommendations"]:
        parts.append("<ul>" + "".join(f"<li>[{escape(r['severity'])}] {escape(r['text'])} <small>({escape(r['rule'])})</small></li>" for r in report["recommendations"]) + "</ul>")
    else:
        parts.append("<p>Рекомендаций нет: " + ("данных мало." if not report["data_sufficiency"]["enough"] else "ни одно правило не сработало.") + "</p>")
    retraining = report["retraining"]
    parts.append("<h2>Журнал переобучения</h2>")
    parts.append(table(["Дата", "Набор данных", "Кандидат модели", "Шлюз", "Утверждение", "Причины"],
                       [[r["created_at"], r["dataset_version"], r["candidate_model_version"], r["gate_status"], r["approval_status"], "; ".join(map(str, r["gate_reasons"]))]
                        for r in retraining["recent"]]))
    if retraining["note"]:
        parts.append(f"<p>{escape(retraining['note'])}</p>")
    parts.append("<p><small>Отклонения в журнале: " + escape(", ".join(f"{k}: {v}" for k, v in retraining["rejection_log_by_retraining_status"].items()) or "нет") + ". "
                 + escape(report["definitions"]["precision_by_inspectors"]) + ".</small></p></body></html>")
    return "".join(parts)
