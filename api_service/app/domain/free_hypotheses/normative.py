"""NORMATIVE -- values already extracted by the matrix mechanisms, checked against the admin-maintained `Normative_Base`
(TZ 9.5, approach 3; TZ-10 table `normative_base`: document, section, parameter, min_value, max_value, effective_from /
effective_to, is_active).

An entry applies to a matrix parameter when its `parameter_name` equals (after normalisation) the parameter's code, matrix
code, catalog name or an alias. A value outside [min_value, max_value] of an entry that is active and in force is a hypothesis
that names the entry's document and section as the normative basis. The value is compared in the unit of the matrix
parameter (the entry has no unit column: the administrator enters the bound in that unit). The detector invents no bounds:
with an empty `normative_base` table it produces nothing (the delivered database ships no seeded norms; the TZ example --
a room 2.4 m high against 2.5 m -- needs a room-height value that no matrix mechanism reads yet).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ...db.models import InspectionProcess, NormativeBaseEntry, Param
from .base import (
    CRITICALITY_SUBSTANTIAL,
    METHOD_NORMATIVE,
    HypothesisCandidate,
    make_dedup_key,
    norm_token,
)
from .logic_rules import fragment_ref, stage_values


def _basis(entry: NormativeBaseEntry) -> str:
    bits = [entry.document_name]
    if entry.document_number:
        bits.append(str(entry.document_number))
    text = " ".join(str(b) for b in bits if b)
    return f"{text}, {entry.section}" if entry.section else text


def _param_names(param: Param) -> set[str]:
    names = {norm_token(param.code), norm_token(param.matrix_code), norm_token(param.parameter_name)}
    for alias in param.aliases_json or []:
        names.add(norm_token(alias))
    return {n for n in names if n}


def _in_force(entry: NormativeBaseEntry, now: datetime) -> bool:
    def utc_naive(value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    now = utc_naive(now) or datetime.utcnow()
    effective_from = utc_naive(entry.effective_from)
    effective_to = utc_naive(entry.effective_to)
    return bool(entry.is_active) and (effective_from is None or effective_from <= now) and (effective_to is None or effective_to >= now)


def detect(db: Session, process: InspectionProcess, params: list[Param], *, object_id: str | None, now: datetime | None = None) -> tuple[list[HypothesisCandidate], dict[str, Any]]:
    now = now or datetime.utcnow()
    entries = [e for e in db.query(NormativeBaseEntry).order_by(NormativeBaseEntry.id).all()
               if _in_force(e, now) and e.parameter_name and (e.min_value is not None or e.max_value is not None)]
    stats: dict[str, Any] = {"entries_in_force": len(entries), "entries_matched": 0, "values_checked": 0}
    out: list[HypothesisCandidate] = []
    by_name: dict[str, list[Param]] = {}
    for param in params:
        for name in _param_names(param):
            by_name.setdefault(name, []).append(param)
    for entry in entries:
        matched = by_name.get(norm_token(entry.parameter_name)) or []
        if not matched:
            continue
        stats["entries_matched"] += 1
        for param in matched:
            for stage, (value, group) in stage_values(db, process, param.code).items():
                stats["values_checked"] += 1
                below = entry.min_value is not None and value < entry.min_value
                above = entry.max_value is not None and value > entry.max_value
                if not (below or above):
                    continue
                ref = fragment_ref(db, group, stage)
                if ref is None:
                    continue
                ref.role = "expected" if stage == "PD" else "actual"
                bound = f"не менее {entry.min_value:g}" if below else f"не более {entry.max_value:g}"
                unit = f" {param.unit}" if param.unit else ""
                basis = _basis(entry)
                out.append(HypothesisCandidate(
                    method=METHOD_NORMATIVE, detector=f"NB-{int(entry.id)}",
                    dedup_key=make_dedup_key(object_id, METHOD_NORMATIVE, int(entry.id), param.code, stage, f"{value:g}"),
                    description=f"{param.parameter_name} ({stage}): {value:g}{unit}; по нормативной базе требуется {bound}{unit} ({basis}).",
                    confidence=0.6, evidence=[ref], criticality=CRITICALITY_SUBSTANTIAL, location=stage,
                    expected=f"{bound}{unit}", actual=f"{value:g}{unit}", normative_base=basis,
                    details={"rule_name": f"Норматив: {entry.parameter_name}", "entry_id": int(entry.id), "param_code": param.code, "stage": stage,
                             "min_value": entry.min_value, "max_value": entry.max_value, "evidence_required": []},
                    editions=(int(ref.document.id),)))
    stats["hypotheses"] = len(out)
    return out, stats
