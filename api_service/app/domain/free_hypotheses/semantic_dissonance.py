"""SEMANTIC_DISSONANCE -- a contradiction in terminology between PD and RD (TZ 9.5, approach 2).

TZ example: the room is «Техническое» in the project documentation and «Склад ГСМ» in the working documentation.

Input: the rows of PD / RD schedules that `explication_compare` paired by room key (same floor / section scope, shared
keys) and whose names differ after the typo / wrapped-cell tolerance. For each such pair:

  * `sim`   -- cosine similarity of the two names in the multilingual MiniLM space (`semantic_similarity`);
  * `class` -- a functional class derived from the object's *own* names: every distinct name printed in the PD and RD
               schedules of the object is greedily clustered by embedding (cluster centroid cosine >= CLASS_JOIN);
               no list of room types and nothing object-specific is hard-coded;
  * the pair is a dissonance when the names do not share a stem, `sim` < DISSONANCE_MAX_SIM and the classes differ.

The ranking score grows with `1 - sim`; it is not a probability. Without the embedding model the detector abstains
and says so in its diagnostics (no lexical fallback is invented).
Thresholds were calibrated on the development objects (see evaluation/phase12/FINAL_B2_HYPOTHESES_REPORT.md), not on
hidden ones.
"""
from __future__ import annotations

import os
import re
from typing import Any

from .. import explication_compare as ec
from .. import semantic_similarity as sem
from .. import table_parser as tpar
from .base import (
    CRITICALITY_SUBSTANTIAL,
    METHOD_SEMANTIC,
    HypothesisCandidate,
    make_dedup_key,
    norm_token,
)
from .tables import ranking_confidence, row_evidence, scope_label

DETECTOR = "semantic_dissonance.room_name"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def dissonance_max_sim() -> float:
    return _float_env("CASE10_SEMANTIC_DISSONANCE_MAX_SIM", 0.72)


def class_join() -> float:
    return _float_env("CASE10_SEMANTIC_CLASS_JOIN", 0.78)


_WORD_RE = re.compile(r"[а-яёa-z]{3,}", re.IGNORECASE)


def _stems(text: str) -> set[str]:
    return {w.lower().replace("ё", "е")[:5] for w in _WORD_RE.findall(text or "")}


def handles(d: ec.Discrepancy) -> bool:
    return bool(d.violation) and (d.type == ec.TYPE_NAME_CHANGED or (d.type == ec.TYPE_COMPOSITION_CHANGED and d.field in tpar.TEXT_ROLES))


def _all_names(results: list[ec.PairResult]) -> list[str]:
    names: dict[str, int] = {}
    for result in results:
        for ref in (result.pd, result.rd):
            for row in ref.table.rows:
                values = tpar.row_values(ref.table, row)
                text = values.get(tpar.ROLE_NAME) or values.get(tpar.ROLE_TYPE)
                if text:
                    key = " ".join(str(text).split())
                    names[key] = names.get(key, 0) + 1
    return [name for name, _ in sorted(names.items(), key=lambda item: (-item[1], item[0]))]


def functional_classes(names: list[str]) -> dict[str, int]:
    """name -> class id; greedy clustering of the object's own distinct names by embedding (deterministic order)."""
    centroids: list[Any] = []
    assigned: dict[str, int] = {}
    join = class_join()
    for name in names:
        vec = sem.embed(name)
        if vec is None:
            continue
        best, best_sim = None, -1.0
        for index, centroid in enumerate(centroids):
            sim = sem.cosine(vec, centroid)
            if sim > best_sim:
                best, best_sim = index, sim
        if best is not None and best_sim >= join:
            assigned[name] = best
        else:
            centroids.append(vec)
            assigned[name] = len(centroids) - 1
    return assigned


def detect(results: list[ec.PairResult], *, object_id: str | None, already_reported: set[tuple]) -> tuple[list[HypothesisCandidate], dict[str, Any]]:
    stats: dict[str, Any] = {"name_changes_seen": 0, "skipped_reported_by_matrix": 0, "not_dissonant": 0, "model": sem.SEMANTIC_MODEL_NAME,
                             "max_sim": dissonance_max_sim(), "class_join": class_join()}
    pairs = [(result, d) for result in results for d in result.discrepancies if handles(d)]
    stats["name_changes_seen"] = len(pairs)
    if not pairs:
        stats["hypotheses"] = 0
        return [], stats
    if not sem.is_enabled() or sem.embed("тест") is None:
        stats["abstained"] = "semantic_model_unavailable"
        stats["hypotheses"] = 0
        return [], stats
    classes = functional_classes(_all_names(results))
    out: list[HypothesisCandidate] = []
    for result, d in pairs:
        if (norm_token(d.location), str(d.field), d.type, norm_token(d.expected), norm_token(d.actual)) in already_reported:
            stats["skipped_reported_by_matrix"] += 1
            continue
        pd_name, rd_name = " ".join(str(d.expected or "").split()), " ".join(str(d.actual or "").split())
        sim = sem.similarity(pd_name, rd_name)
        if sim is None:
            continue
        pd_class, rd_class = classes.get(pd_name), classes.get(rd_name)
        shared_stem = bool(_stems(pd_name) & _stems(rd_name))
        if shared_stem or sim >= dissonance_max_sim() or (pd_class is not None and pd_class == rd_class):
            stats["not_dissonant"] += 1
            continue
        base = ranking_confidence(result, d)
        confidence = round(min(0.95, max(0.2, base * (0.6 + 0.4 * (1.0 - sim)))), 3)
        kind = "квартиры" if result.pd.table.kind == tpar.KIND_APARTMENTS else "помещения"
        scope = scope_label(result)
        description = (
            f"Название {kind} {d.location}" + (f" ({scope})" if scope else "") +
            f" различается по смыслу: в экспликации ПД — «{pd_name}», в РД — «{rd_name}» "
            f"(косинусное сходство названий {sim:.2f}; функциональные классы различны). Возможна смена назначения помещения."
        )
        out.append(HypothesisCandidate(
            method=METHOD_SEMANTIC, detector=DETECTOR,
            dedup_key=make_dedup_key(object_id, METHOD_SEMANTIC, scope, d.location, d.field, pd_name, rd_name),
            description=description, confidence=confidence, criticality=CRITICALITY_SUBSTANTIAL,
            evidence=[row_evidence(result.pd, d.pd_row, role="expected", value=pd_name, confidence=confidence),
                      row_evidence(result.rd, d.rd_row, role="actual", value=rd_name, confidence=confidence)],
            location=d.location, expected=pd_name, actual=rd_name, normative_base=None,
            details={"rule_name": "Семантический диссонанс названий (ПД ↔ РД)", "similarity": round(sim, 4), "pd_class": pd_class, "rd_class": rd_class,
                     "matched_by": d.details.get("matched_by"), "pair_score": result.score,
                     "scope": {"PD": result.pd.table.scope.to_dict(), "RD": result.rd.table.scope.to_dict()},
                     "revision_alternatives": [alt.file_id for alt in result.alternatives], "evidence_required": ["PD", "RD"]},
            editions=tuple(sorted({int(result.pd.doc_key), int(result.rd.doc_key)})),
        ))
    stats["hypotheses"] = len(out)
    return out, stats
