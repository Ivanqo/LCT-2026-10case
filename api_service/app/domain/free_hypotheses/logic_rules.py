"""LOGICAL_ANALYSIS, second family: rules of *internal consistency* ("if A, then B must agree") -- TZ 9.5, approach 1.

The three original rules (`logical_analysis.LOGICAL_RULES`, "A present -> B must be evidenced") are untouched. The rules
here claim no norm: each states a relation that must hold between values of ONE stage of ONE object, and fires only
when both sides were actually read (this run's evidence groups, or this object's parsed schedules); a missing value is an
abstention, never a hypothesis. A page of poor quality is never the source of an absence claim (the table scan already
drops garbled tables).

  LR-101  total construction volume = underground + above-ground volume          (matrix values, per stage)
  LR-102  useful / design area is not larger than the total building area        (matrix values, per stage)
  LR-103  building height per above-ground floor is not below 2.5 m              (matrix values, per stage)
  LR-104  above-ground volume is not larger than footprint area x height          (matrix values, per stage)
  LR-105  the printed total of a room schedule equals the sum of its room areas  (parsed schedules, per table)
  LR-106  the schedule lists no more apartments than the declared apartment count (matrix value + parsed schedules, PD)

`normative_base` is "не определено" for all of them: they are arithmetic / geometric consistency, not norm compliance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session

from ...db.models import EvidenceFragment, EvidenceGroup, InspectionProcess, Param
from .. import explication_compare as ec
from .. import table_parser as tpar
from .base import (
    CRITICALITY_SUBSTANTIAL,
    METHOD_LOGICAL,
    EvidenceRef,
    HypothesisCandidate,
    make_dedup_key,
)

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
MAX_TOTAL_GAP_SHARE = Decimal("0.15")      # LR-105 claims a slip only while the total is within 15 % of the sum of rows
_APARTMENT_KEY_RE = re.compile(r"^(кв\.? ?\S+?)(?: пом\.|$)", re.IGNORECASE)
_STAGE_BY_FRAGMENT = {"project": "PD", "working": "RD", "as_built": "ID"}


@dataclass(frozen=True)
class RelationRule:
    rule_id: str
    rule_name: str
    inputs: tuple[str, ...]                                   # matrix parameter codes, read per stage
    explanation: str                                          # what must hold, in words
    check: Callable[[dict[str, float]], tuple[bool, str, str] | None]   # values by code -> (violated, expected text, actual text)
    confidence: float
    is_active: bool = True
    criticality: str = CRITICALITY_SUBSTANTIAL


def _volume(v: dict[str, float]):
    total, under, above = v["PZ-004"], v["PZ-005"], v["PZ-006"]
    if total <= 0:
        return None
    gap = abs(total - (under + above))
    return gap / total > 0.02, f"общий объём {total:g} м³ = подземный {under:g} + надземный {above:g} = {under + above:g} м³", f"расхождение {gap:g} м³ ({gap / total * 100:.1f} %)"


def _areas(v: dict[str, float]):
    useful, total = v["PZ-003"], v["PZ-002"]
    if total <= 0 or useful <= 0:
        return None
    return useful > total * 1.01, f"полезная/расчётная площадь {useful:g} м² не больше общей площади здания {total:g} м²", f"превышение {useful - total:g} м²"


def _height(v: dict[str, float]):
    height, floors = v["PZ-008"], v["PZ-007"]
    if floors < 1 or height <= 0:
        return None
    per_floor = height / floors
    return per_floor < 2.5, f"высота здания {height:g} м при {floors:g} эт.: на этаж не менее 2,5 м", f"{per_floor:.2f} м на этаж"


def _footprint_volume(v: dict[str, float]):
    above, footprint, height = v["PZ-006"], v["PZ-001"], v["PZ-008"]
    if min(above, footprint, height) <= 0:
        return None
    bound = footprint * height * 1.15
    return above > bound, f"надземный объём {above:g} м³ не больше площади застройки {footprint:g} м² × высота {height:g} м (с запасом 15 %) = {bound:g} м³", f"превышение {above - bound:g} м³"


RELATION_RULES: tuple[RelationRule, ...] = (
    RelationRule("LR-101", "Строительный объём: общий = подземный + надземный", ("PZ-004", "PZ-005", "PZ-006"),
                 "общий строительный объём равен сумме подземного и надземного (допуск 2 %)", _volume, 0.5),
    RelationRule("LR-102", "Площади: полезная не больше общей", ("PZ-002", "PZ-003"),
                 "полезная/расчётная площадь не превышает общую площадь здания", _areas, 0.4),
    RelationRule("LR-103", "Высота здания и этажность", ("PZ-007", "PZ-008"),
                 "на один надземный этаж приходится не менее 2,5 м высоты здания", _height, 0.4),
    RelationRule("LR-104", "Надземный объём не больше площади застройки на высоту", ("PZ-001", "PZ-006", "PZ-008"),
                 "надземный объём не превышает произведения площади застройки на высоту здания (запас 15 %)", _footprint_volume, 0.4),
)


SCHEDULE_RULES: tuple[tuple[str, str, str, str, float], ...] = (
    ("LR-105", "Итог экспликации = сумма площадей помещений", "В экспликации помещений напечатан итог по таблице",
     "итог равен сумме площадей строк таблицы (допуск 1 %)", 0.4),
    ("LR-106", "Число квартир: ТЭП ПД и экспликации ПД", "В ТЭП ПД объявлено число квартир",
     "в экспликациях квартир ПД перечислено не больше квартир, чем объявлено", 0.4),
)


def rule_descriptors() -> list[dict[str, Any]]:
    """The rules of this module as `Logical_Rules` rows (seeded at startup next to the original three)."""
    rows = [{"rule_id": r.rule_id, "rule_name": r.rule_name, "condition": "Значения параметров " + ", ".join(r.inputs) + " прочитаны в одной стадии",
             "expected": r.explanation, "normative_base": "не определено (внутренняя согласованность)", "criticality": r.criticality,
             "confidence": r.confidence, "is_active": r.is_active} for r in RELATION_RULES]
    rows += [{"rule_id": rid, "rule_name": name, "condition": cond, "expected": exp, "normative_base": "не определено (внутренняя согласованность)",
              "criticality": CRITICALITY_SUBSTANTIAL, "confidence": conf, "is_active": True} for rid, name, cond, exp, conf in SCHEDULE_RULES]
    return rows


def rule_activity(db: Session) -> dict[str, bool]:
    """`is_active` overrides an admin set on the `logical_rules` table; a rule without a row keeps its code default."""
    from ...db.models import LogicalRuleRecord

    return {row.rule_id: bool(row.is_active) for row in db.query(LogicalRuleRecord.rule_id, LogicalRuleRecord.is_active).all()}


# ---------------------------------------------------------------- reading the object's already extracted values


def _first_number(raw: object) -> float | None:
    match = _NUMBER_RE.search(str(raw or "").replace("\xa0", " "))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def stage_values(db: Session, process: InspectionProcess, code: str) -> dict[str, tuple[float, EvidenceGroup]]:
    """{stage: (value, group)} of the matrix parameter `code` in this process. A stage value is read from a group that
    produced a value for that stage (delta.values) and is not a hypothesis or a page-quality warning; the most
    confident group wins, ties by id (deterministic)."""
    groups = (db.query(EvidenceGroup).join(Param, Param.id == EvidenceGroup.param_id)
              .filter(EvidenceGroup.process_id == process.id, Param.code == code).order_by(EvidenceGroup.id).all())
    best: dict[str, tuple[float, EvidenceGroup]] = {}
    for group in groups:
        key = str(group.group_key or "")
        if key.startswith(("suspicion:", "hyp:")) or group.finding_status in ("SUSPICION", "MISSING_EVIDENCE", "CLARIFICATION_REQUIRED"):
            continue
        delta = group.delta if isinstance(group.delta, dict) else {}
        values = delta.get("values") if isinstance(delta.get("values"), dict) else {}
        for stage in ("PD", "RD", "ID"):
            number = _first_number(values.get(stage))
            if number is None:
                continue
            current = best.get(stage)
            if current is None or (group.confidence or 0.0) > (current[1].confidence or 0.0):
                best[stage] = (number, group)
    return best


def fragment_ref(db: Session, group: EvidenceGroup, stage: str) -> EvidenceRef | None:
    rows = (db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == int(group.id), EvidenceFragment.page.isnot(None))
            .order_by(EvidenceFragment.role.asc(), EvidenceFragment.id.asc()).all())
    row = next((r for r in rows if _STAGE_BY_FRAGMENT.get(str(r.stage)) == stage), None)
    if row is None or row.document_version is None:
        return None
    return EvidenceRef(document=row.document_version, page=int(row.page), stage=stage, role="context", bbox_pdf=row.bbox_pdf,
                       page_width=row.page_width, page_height=row.page_height, value=row.extracted_value, context=row.context, confidence=row.confidence)


def detect_relations(db: Session, process: InspectionProcess, params_in_scope: set[str], *, object_id: str | None) -> tuple[list[HypothesisCandidate], dict[str, Any]]:
    out: list[HypothesisCandidate] = []
    stats: dict[str, Any] = {"rules_evaluated": 0, "rules_without_inputs": 0, "scope_complete": True}
    activity = rule_activity(db)
    for rule in RELATION_RULES:
        if not activity.get(rule.rule_id, rule.is_active):
            continue
        if not set(rule.inputs) <= params_in_scope:
            stats["scope_complete"] = False
            continue
        stats["rules_evaluated"] += 1
        per_code = {code: stage_values(db, process, code) for code in rule.inputs}
        fired = False
        for stage in ("PD", "RD", "ID"):
            if not all(stage in per_code[code] for code in rule.inputs):
                continue
            values = {code: per_code[code][stage][0] for code in rule.inputs}
            verdict = rule.check(values)
            if verdict is None:
                continue
            fired = True
            violated, expected, actual = verdict
            if not violated:
                continue
            refs = [r for r in (fragment_ref(db, per_code[code][stage][1], stage) for code in rule.inputs) if r is not None]
            if not refs:
                continue
            for ref in refs:
                ref.role = "actual" if stage != "PD" else "expected"
            out.append(HypothesisCandidate(
                method=METHOD_LOGICAL, detector=rule.rule_id,
                dedup_key=make_dedup_key(object_id, METHOD_LOGICAL, rule.rule_id, stage, *[f"{values[c]:g}" for c in rule.inputs]),
                description=f"{rule.rule_name} ({stage}): {rule.explanation}. Прочитано: " + "; ".join(f"{c} = {values[c]:g}" for c in rule.inputs) + f". Не выполняется: {actual}.",
                confidence=rule.confidence, evidence=refs, criticality=rule.criticality, location=stage, expected=expected, actual=actual,
                normative_base=None,
                details={"rule_name": rule.rule_name, "family": "internal_consistency", "stage": stage, "inputs": dict(values),
                         "evidence_required": [], "note": "арифметическая/геометрическая согласованность, норма не заявляется"},
                editions=tuple(sorted({int(r.document.id) for r in refs})),
            ))
        if not fired:
            stats["rules_without_inputs"] += 1
    stats["hypotheses"] = len(out)
    return out, stats


# ---------------------------------------------------------------- schedule rules (parsed room / apartment tables)


def _decimal_sum(table: tpar.RoomTable, role: str) -> tuple[Decimal, int] | None:
    total = Decimal(0)
    count = 0
    for row in table.rows:
        cell = tpar.row_values(table, row).get(role)
        if cell is None:
            continue
        total += cell["value"]
        count += 1
    return (total, count) if count else None


def _sequence_has_gap(table: tpar.RoomTable) -> bool:
    """True when the plain-numbered rows of the table skip a number inside their own range (001, 002, 004, ...) or do not start
    at 1: a row the parser missed, so a sum over the parsed rows proves nothing about the printed total."""
    numbers = sorted({int(row.key) for row in table.rows if str(row.key).strip().isdigit()})
    if len(numbers) < 3:
        return False
    # a schedule numbers its rows from 1 (001): a table that starts higher lost its upper rows (sub-blocks per apartment)
    return numbers[0] > 1 or numbers[-1] - numbers[0] + 1 != len(numbers)


def detect_schedule_rules(refs: dict[str, list[ec.TableRef]], *, object_id: str | None, db: Session, process: InspectionProcess,
                          params_in_scope: set[str]) -> tuple[list[HypothesisCandidate], dict[str, Any]]:
    out: list[HypothesisCandidate] = []
    stats: dict[str, Any] = {"lr105_tables_checked": 0, "lr106_checked": False}
    activity = rule_activity(db)
    # LR-105: a schedule's printed total vs the sum of its rows (single-block table, total printed once)
    for stage in (("PD", "RD") if activity.get("LR-105", True) else ()):
        for ref in refs.get(stage, []):
            table = ref.table
            if table.kind != tpar.KIND_ROOMS or len(table.totals) != 1 or table.blocks != 1 or len(table.rows) < 3 or table.notes:
                continue
            role = tpar.ROLE_AREA if table.column_index(tpar.ROLE_AREA) is not None else None
            if role is None:
                continue
            total, _all, decimals = tpar.total_value(table.totals[0])
            summed = _decimal_sum(table, role)
            if total is None or summed is None or total <= 0:
                continue
            if _sequence_has_gap(table):
                stats["lr105_incomplete_parse"] = stats.get("lr105_incomplete_parse", 0) + 1
                continue
            stats["lr105_tables_checked"] += 1
            gap = abs(summed[0] - total)
            tolerance = max(total * Decimal("0.01"), Decimal(1).scaleb(-decimals) * 2)
            if gap <= tolerance:
                continue
            if gap > total * MAX_TOTAL_GAP_SHARE:
                # a "total" far from the sum is a misread cell (a floor number, a neighbouring column), not an arithmetic
                # slip in the document: abstain instead of claiming it
                stats["lr105_unreliable_total"] = stats.get("lr105_unreliable_total", 0) + 1
                continue
            scope = ref.table.scope.label()
            expected = f"сумма площадей {summed[1]} строк = {summed[0]:f} м²"
            actual = f"итог в таблице {' '.join(table.totals[0].values)}"
            evidence = [EvidenceRef(document=ref.document, page=ref.page, stage=stage, role="expected" if stage == "PD" else "actual",
                                    bbox_pdf=list(table.totals[0].bbox), page_width=table.width, page_height=table.height,
                                    value=actual, context=table.totals[0].label, confidence=0.5),
                        EvidenceRef(document=ref.document, page=ref.page, stage=stage, role="context", bbox_pdf=list(table.bbox),
                                    page_width=table.width, page_height=table.height, value=expected, context=table.title, confidence=0.5)]
            out.append(HypothesisCandidate(
                method=METHOD_LOGICAL, detector="LR-105",
                dedup_key=make_dedup_key(object_id, METHOD_LOGICAL, "LR-105", stage, scope, f"{summed[0]:f}", f"{total:f}"),
                description=f"Экспликация {stage}{' (' + scope + ')' if scope else ''}: итог по таблице не равен сумме площадей помещений — {expected}, {actual} (разница {gap:f} м²). "
                            "Возможны опечатка в итоге, не учтённое помещение или ошибка разбора строк — проверить по листу.",
                confidence=0.4, evidence=evidence, criticality=CRITICALITY_SUBSTANTIAL, location=scope or ref.table.title or "экспликация",
                expected=expected, actual=actual, normative_base=None,
                details={"rule_name": "Итог экспликации ≠ сумма площадей", "family": "internal_consistency", "stage": stage,
                         "rows": summed[1], "evidence_required": [stage]},
                editions=(int(ref.doc_key),),
            ))
    # LR-106: PD schedules list more distinct apartments than the declared apartment count (one-sided: a partial parse cannot cause it)
    if "PZ-010" in params_in_scope and activity.get("LR-106", True):
        declared = stage_values(db, process, "PZ-010").get("PD")
        if declared is not None:
            number, group = declared
            numbers: set[str] = set()
            for ref in refs.get("PD", []):
                if ref.table.kind != tpar.KIND_APARTMENTS:
                    continue
                for row in ref.table.rows:
                    match = _APARTMENT_KEY_RE.match(str(row.key))
                    numbers.add((ref.table.scope.building or "", match.group(1) if match else tpar.normalize_key(row.key)).__repr__())
            stats["lr106_checked"] = True
            if len(numbers) > number * 1.0 + 0.5 and number > 0:
                ref0 = fragment_ref(db, group, "PD")
                if ref0 is not None:
                    ref0.role = "expected"
                    schedule = next(r for r in refs["PD"] if r.table.kind == tpar.KIND_APARTMENTS)
                    evidence = [ref0, EvidenceRef(document=schedule.document, page=schedule.page, stage="PD", role="actual", bbox_pdf=list(schedule.table.bbox),
                                                  page_width=schedule.table.width, page_height=schedule.table.height,
                                                  value=f"{len(numbers)} квартир", context=schedule.table.title, confidence=0.4)]
                    out.append(HypothesisCandidate(
                        method=METHOD_LOGICAL, detector="LR-106",
                        dedup_key=make_dedup_key(object_id, METHOD_LOGICAL, "LR-106", f"{number:g}", len(numbers)),
                        description=f"В ПД объявлено {number:g} квартир, а в экспликациях квартир ПД перечислено {len(numbers)} различных квартир.",
                        confidence=0.4, evidence=evidence, criticality=CRITICALITY_SUBSTANTIAL, location="ПД", expected=f"{number:g} квартир",
                        actual=f"{len(numbers)} квартир в экспликациях", normative_base=None,
                        details={"rule_name": "Число квартир: ТЭП ПД и экспликации ПД", "family": "internal_consistency", "evidence_required": ["PD"]},
                        editions=tuple(sorted({int(ref0.document.id), int(schedule.doc_key)}))))
    stats["hypotheses"] = len(out)
    return out, stats
