"""UNMATCHED_DIFF -- a difference between the stages that no matrix parameter describes (TZ 9.5, priority 1 for recall).

Experts: "найти расхождение важнее, чем классифицировать". The room / apartment schedules of PD and RD are paired by
the shared room keys, floor / section scope and names (`explication_compare`), and every cell that differs after
normalisation (printed precision, a struck value printed next to the new one) is a hypothesis with both sources and boxes:

  * the area of a room / apartment differs (above the rounding noise of the printed precision and this detector's own 1 % noise floor);
  * the number of rooms / apartments in a row differs;
  * a room is listed in one stage only;
  * the floor total differs.

The 1 % is this detector's noise floor for a printed area, **not** a threshold taken over from M-002: the hypothesis says so
("порог не установлен матрицей") and states the deviation. Name / function changes belong to SEMANTIC_DISSONANCE.
Rows of different rooms / floors are never joined by a number alone: the pair needs the table scope, shared keys and
the name agreement `explication_compare.pair_score` demands. A difference S1 already reported as a matrix finding
(flag CASE10_EXPLICATION_COMPARE=1) is not repeated.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .. import explication_compare as ec
from .. import table_parser as tpar
from .base import (
    CRITICALITY_MINOR,
    CRITICALITY_SUBSTANTIAL,
    METHOD_UNMATCHED,
    HypothesisCandidate,
    make_dedup_key,
)
from .tables import ranking_confidence, row_evidence, scope_label, table_evidence

DETECTOR_AREA = "unmatched_diff.area"
DETECTOR_COUNT = "unmatched_diff.count"
DETECTOR_PRESENCE = "unmatched_diff.presence"
DETECTOR_TOTAL = "unmatched_diff.total"
SMALL_AREA_RELATIVE = Decimal("0.03")           # below: the claim is real but minor
NOISE_FLOOR_RELATIVE = Decimal("0.01")          # below: not reported at all (printing / rounding noise of a schedule). A noise filter
                                                # of this detector, not a norm and not the M-002 threshold

_FIELD_TITLES = {
    tpar.ROLE_AREA: "площадь", tpar.ROLE_LIVING_AREA: "жилая площадь", tpar.ROLE_TOTAL_AREA: "общая площадь",
    tpar.ROLE_SUMMER_AREA: "площадь с коэффициентом", tpar.ROLE_COUNT: "количество", tpar.ROLE_ROOMS: "число комнат",
}


def handles(d: ec.Discrepancy) -> bool:
    """Everything except a name / function change (SEMANTIC_DISSONANCE) and a table-equal marker."""
    if d.type == ec.TYPE_TABLE_EQUAL or not d.violation:
        return False
    if d.type in (ec.TYPE_NAME_CHANGED,) or (d.type == ec.TYPE_COMPOSITION_CHANGED and d.field in tpar.TEXT_ROLES):
        return False
    return True


def _where(result: ec.PairResult, d: ec.Discrepancy) -> str:
    scope = scope_label(result)
    kind = "квартира" if result.pd.table.kind == tpar.KIND_APARTMENTS else "помещение"
    return f"{kind} {d.location}" + (f" ({scope})" if scope else "")


def _describe(result: ec.PairResult, d: ec.Discrepancy) -> tuple[str, str, str]:
    """(detector id, description, deviation text)"""
    where = _where(result, d)
    pd_file, rd_file = result.pd.file_id, result.rd.file_id
    if d.type == ec.TYPE_AREA_CHANGED:
        relative = Decimal(d.details["relative"]) if d.details.get("relative") else None
        deviation = f"отклонение {d.details.get('delta')} м²" + (f" ({(relative * 100).quantize(Decimal('0.1'))} %)" if relative is not None else "")
        title = _FIELD_TITLES.get(d.field or "", "площадь")
        return DETECTOR_AREA, (
            f"{where}: {title} в экспликации ПД — {d.expected} м², в РД — {d.actual} м² ({deviation}). "
            "Параметр Матрицы не определён: порог отклонения для площади помещения матрицей не установлен "
            "(учтён только шум печатной точности и 1 %), вывод за инспектором."), deviation
    if d.type == ec.TYPE_COUNT_CHANGED or (d.type == ec.TYPE_COMPOSITION_CHANGED and d.field in (tpar.ROLE_COUNT, tpar.ROLE_ROOMS)):
        title = _FIELD_TITLES.get(d.field or "", "значение")
        deviation = f"отклонение {d.details.get('delta')}"
        return DETECTOR_COUNT, f"{where}: {title} в ПД — {d.expected}, в РД — {d.actual} ({deviation}). Параметр Матрицы не определён.", deviation
    if d.type == ec.TYPE_ONLY_PD:
        return DETECTOR_PRESENCE, f"{where} есть в экспликации ПД ({d.expected}), но не найдено в экспликации РД ({rd_file}, стр. {result.rd.page}).", "нет в РД"
    if d.type == ec.TYPE_ONLY_RD:
        return DETECTOR_PRESENCE, f"{where} есть в экспликации РД ({d.actual}), но не найдено в экспликации ПД ({pd_file}, стр. {result.pd.page}).", "нет в ПД"
    if d.type == ec.TYPE_TOTAL:
        deviation = f"отклонение {d.details.get('delta')} ({(Decimal(d.details['relative']) * 100).quantize(Decimal('0.1'))} %)" if d.details.get("relative") else "отклонение итога"
        return DETECTOR_TOTAL, f"Итог по экспликации ({where or 'таблица'}): ПД — {d.expected}, РД — {d.actual} ({deviation}). Итог по экспликации помещений не есть параметр Матрицы M-002 (итог по зданию).", deviation
    return DETECTOR_AREA, f"{where}: ПД — {d.expected}, РД — {d.actual}.", ""


def _row_visible_below_table(ref: ec.TableRef, key: str) -> bool:
    """A "room is absent from the schedule" claim is refuted when the room number is printed in the number column of the same
    schedule -- inside the part the parser read (a duplicated key dropped the row) or below it (a wrapped cell or a page break
    ended the table early). The page text is read again; where it cannot be read (no original PDF) the claim stays, unverified."""
    from .. import dataset_sources

    try:
        snapshot = dataset_sources.extract_original_pages(ref.document, {ref.page}).get(ref.page)
    except Exception:  # noqa: BLE001
        return False
    if not snapshot:
        return False
    column = next((c for c in ref.table.columns if c.role == tpar.ROLE_NUMBER), None)
    if column is None:
        return False
    wanted = tpar.normalize_key(key)
    top, bottom = float(ref.table.bbox[1]), float(ref.table.bbox[3])
    for word in snapshot.get("words") or []:
        x0, y0, x1, _ = (float(v) for v in word["bbox"])
        if column.x0 - 8 <= x0 and x1 <= column.x1 + 8 and top - 2 <= y0 <= bottom + 700 and tpar.normalize_key(word.get("text")) == wanted:
            return True
    return False


def detect(results: list[ec.PairResult], *, object_id: str | None, already_reported: set[tuple]) -> tuple[list[HypothesisCandidate], dict[str, Any]]:
    """`already_reported`: S1 matrix findings as (location, field, type, expected, actual) -- not repeated."""
    from .base import norm_token

    out: list[HypothesisCandidate] = []
    stats = {"differences_seen": 0, "skipped_reported_by_matrix": 0}
    for result in results:
        for d in result.discrepancies:
            if not handles(d):
                continue
            stats["differences_seen"] += 1
            if d.type == ec.TYPE_AREA_CHANGED and d.details.get("relative") is not None and Decimal(d.details["relative"]) < NOISE_FLOOR_RELATIVE:
                stats["below_noise_floor"] = stats.get("below_noise_floor", 0) + 1
                continue
            if (norm_token(d.location), str(d.field), d.type, norm_token(d.expected), norm_token(d.actual)) in already_reported:
                stats["skipped_reported_by_matrix"] += 1
                continue
            if d.type in (ec.TYPE_ONLY_PD, ec.TYPE_ONLY_RD):
                other, row = (result.rd, d.pd_row) if d.type == ec.TYPE_ONLY_PD else (result.pd, d.rd_row)
                if row is not None and _row_visible_below_table(other, row.key):
                    stats["presence_refuted_key_printed_in_table"] = stats.get("presence_refuted_key_printed_in_table", 0) + 1
                    continue
            detector, description, deviation = _describe(result, d)
            confidence = ranking_confidence(result, d)
            if d.type == ec.TYPE_AREA_CHANGED and d.details.get("relative") is not None:
                confidence = round(confidence * (0.7 + 0.3 * min(1.0, float(Decimal(d.details["relative"]) / Decimal("0.10")))), 3)
            evidence = []
            if d.pd_row is not None:
                evidence.append(row_evidence(result.pd, d.pd_row, role="expected", value=d.expected, confidence=confidence))
            else:
                evidence.append(table_evidence(result.pd, role="expected", value="нет строки в экспликации ПД", confidence=confidence))
            if d.rd_row is not None:
                evidence.append(row_evidence(result.rd, d.rd_row, role="actual", value=d.actual, confidence=confidence))
            else:
                evidence.append(table_evidence(result.rd, role="actual", value="нет строки в экспликации РД", confidence=confidence))
            relative = Decimal(d.details["relative"]) if d.details.get("relative") else None
            criticality = CRITICALITY_MINOR if (d.type == ec.TYPE_AREA_CHANGED and relative is not None and relative < SMALL_AREA_RELATIVE) else CRITICALITY_SUBSTANTIAL
            out.append(HypothesisCandidate(
                method=METHOD_UNMATCHED, detector=detector,
                dedup_key=make_dedup_key(object_id, METHOD_UNMATCHED, scope_label(result), d.location, d.type, d.field, d.expected, d.actual),
                description=description, confidence=confidence, evidence=evidence, criticality=criticality,
                location=d.location, expected=d.expected, actual=d.actual, normative_base=None,
                details={"rule_name": "Расхождение вне Матрицы (экспликация ПД ↔ РД)", "discrepancy_type": d.type, "field": d.field,
                         "deviation": deviation, "matched_by": d.details.get("matched_by"), "pair_score": result.score,
                         "scope": {"PD": result.pd.table.scope.to_dict(), "RD": result.rd.table.scope.to_dict()},
                         "noise_floor": "1 % и единица печатной точности (округление); не порог матрицы",
                         "revision_alternatives": [alt.file_id for alt in result.alternatives],
                         "evidence_required": ["PD", "RD"]},
                editions=tuple(sorted({int(result.pd.doc_key), int(result.rd.doc_key)})),
            ))
    stats["hypotheses"] = len(out)
    return out, stats
