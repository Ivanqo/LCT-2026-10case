"""ML_PATTERN_STAT -- an honest statistical baseline for TZ 9.5 approach 4 ("ML-паттерн-анализ"); OFF by default.

It is *not* a trained model and learns nothing from historical objects: inside one object and one stage, rooms with the same
(normalised) name are expected to have comparable areas; a robust z-score (median / MAD) over at least MIN_GROUP rooms flags
areas more than Z_LIMIT robust deviations from the group median. The description says "статистическое отклонение, не
обученная модель". Enable with CASE10_FREE_HYPOTHESES_ML=1.
"""
from __future__ import annotations

from statistics import median
from typing import Any

from .. import explication_compare as ec
from .. import table_parser as tpar
from .base import CRITICALITY_MINOR, METHOD_ML, HypothesisCandidate, make_dedup_key, norm_token
from .tables import row_evidence

MIN_GROUP = 6
Z_LIMIT = 4.5
MAD_TO_SIGMA = 1.4826
MIN_RELATIVE_SPREAD = 0.5            # and the area must differ from the median by at least this share of it


def detect(refs: dict[str, list[ec.TableRef]], *, object_id: str | None) -> tuple[list[HypothesisCandidate], dict[str, Any]]:
    out: list[HypothesisCandidate] = []
    stats: dict[str, Any] = {"groups_checked": 0}
    for stage in ("PD", "RD"):
        groups: dict[str, list[tuple[float, ec.TableRef, Any]]] = {}
        for ref in refs.get(stage, []):
            if ref.table.kind != tpar.KIND_ROOMS:
                continue
            for row in ref.table.rows:
                values = tpar.row_values(ref.table, row)
                name, area = values.get(tpar.ROLE_NAME), values.get(tpar.ROLE_AREA)
                if name and area is not None and area["value"] > 0:
                    groups.setdefault(norm_token(name), []).append((float(area["value"]), ref, row))
        for name_key, members in sorted(groups.items()):
            if len(members) < MIN_GROUP:
                continue
            stats["groups_checked"] += 1
            areas = [a for a, _, _ in members]
            center = median(areas)
            mad = median(abs(a - center) for a in areas) * MAD_TO_SIGMA
            if mad <= 0 or center <= 0:
                continue
            for area, ref, row in members:
                z = (area - center) / mad
                if abs(z) < Z_LIMIT or abs(area - center) < MIN_RELATIVE_SPREAD * center:
                    continue
                name = tpar.row_values(ref.table, row)[tpar.ROLE_NAME]
                out.append(HypothesisCandidate(
                    method=METHOD_ML, detector="ml_stat.area_outlier",
                    dedup_key=make_dedup_key(object_id, METHOD_ML, stage, name_key, row.key, f"{area:g}"),
                    description=f"Помещение {row.key} «{name}» ({stage}): площадь {area:g} м² сильно отличается от медианы {center:g} м² "
                                f"помещений с тем же названием ({len(members)} шт., робастная z-оценка {z:.1f}). Статистическое отклонение, не обученная модель.",
                    confidence=0.3, evidence=[row_evidence(ref, row, role="expected" if stage == "PD" else "actual", value=f"{area:g}", confidence=0.3)],
                    criticality=CRITICALITY_MINOR, location=str(row.key), expected=f"≈{center:g} м² (медиана)", actual=f"{area:g} м²", normative_base=None,
                    details={"rule_name": "Статистический выброс площади", "z": round(z, 2), "median": center, "group_size": len(members),
                             "stage": stage, "evidence_required": []},
                    editions=(int(ref.doc_key),)))
    stats["hypotheses"] = len(out)
    return out, stats
