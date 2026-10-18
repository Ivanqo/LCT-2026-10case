"""LAYER_COMPOSITION detector: the layer stacks of the PD and of the RD of one object compared (see `layer_composition.py`)."""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from .base import (
    CRITICALITY_MINOR,
    CRITICALITY_SUBSTANTIAL,
    METHOD_LAYERS,
    EvidenceRef,
    HypothesisCandidate,
    make_dedup_key,
    norm_token,
)
from .layer_composition import (
    DETECTOR,
    KIND_ADDED,
    KIND_MATERIAL,
    KIND_REMOVED,
    KIND_THICKNESS,
    compare_compositions,
    material_like,
    same_construction,
)


def semantic_min() -> float:
    try:
        return float(os.getenv("CASE10_LAYER_SEMANTIC_MIN", "").strip() or 0.80)
    except ValueError:
        return 0.80


def _signature(comp: dict[str, Any]) -> tuple:
    return tuple((norm_token(layer["name"]), layer["mm"]) for layer in comp["layers"])


def _labels_conflict(pd: dict[str, Any], rd: dict[str, Any]) -> bool:
    return bool(pd.get("label") and rd.get("label") and norm_token(pd["label"]) != norm_token(rd["label"]))


def _layer_ref(doc: Any, comp: dict[str, Any], layer: dict[str, Any] | None, *, stage: str, role: str, value: str, confidence: float) -> EvidenceRef:
    box = (layer or {}).get("bbox") or comp.get("bbox")
    return EvidenceRef(document=doc, page=int(comp["page"]), stage=stage, role=role, bbox_pdf=box, page_width=comp.get("width"), page_height=comp.get("height"),
                       value=value, context="; ".join(f"{l['name']} — {l['mm']} мм" for l in comp["layers"])[:900], confidence=confidence)


def _named_on_page(name: str, comp: dict[str, Any]) -> bool:
    """The layer's own words are all printed on the page of the other side's stack: it is not missing there, it is just not part of
    the stack as parsed (printed without a thickness, in running text, or in another block)."""
    from .text_utils import stems

    wanted = set(stems(name))
    return bool(wanted) and wanted <= set(comp.get("page_stems") or ())


def _fmt(layer: dict[str, Any]) -> str:
    return f"{layer['name']} — {layer['mm']} мм"


def detect(documents: list[Any], gate: Any, *, object_id: str | None) -> tuple[list[HypothesisCandidate], dict[str, Any]]:
    from .. import explication_compare as ec
    from .. import semantic_similarity as sem
    from . import doc_scan

    docs = ec.candidate_documents(list(documents), gate)
    scans = doc_scan.scan_documents(docs, ["layers"])
    pd_items: list[tuple[Any, dict[str, Any]]] = []
    rd_items: list[tuple[Any, dict[str, Any]]] = []
    stats: dict[str, Any] = {"documents": len(docs), "pd_compositions": 0, "rd_compositions": 0, "rd_identical_to_pd": 0, "rd_without_pd_counterpart": 0,
                             "low_quality_pages": 0, "semantic_min": semantic_min()}
    for doc in docs:
        scan = (scans.get(int(doc.id)) or {}).get("layers") or {}
        stats["low_quality_pages"] += len(scan.get("low_quality") or [])
        for comp in scan.get("compositions") or []:
            (pd_items if ec._doc_stage(doc) == "PD" else rd_items).append((doc, comp))
    stats["pd_compositions"], stats["rd_compositions"] = len(pd_items), len(rd_items)
    use_semantic = bool(sem.is_enabled() and sem.embed("тест") is not None)
    stats["semantic_available"] = use_semantic
    seen_rd: set[tuple] = set()
    out: list[HypothesisCandidate] = []
    for rd_doc, rd in rd_items:
        signature = (_signature(rd), norm_token(rd.get("label")))
        if signature in seen_rd:
            continue
        seen_rd.add(signature)
        identical = False
        best = None
        for pd_doc, pd in pd_items:
            if _labels_conflict(pd, rd):
                continue
            if _signature(pd) == _signature(rd):
                identical = True
                break
            screen = compare_compositions(pd, rd, semantic_min=semantic_min(), use_semantic=False)
            if screen["paired"] < 2:
                continue
            result = compare_compositions(pd, rd, semantic_min=semantic_min(), use_semantic=True) if use_semantic else screen
            if not result["diffs"]:
                identical = True
                break
            if not same_construction(result, pd, rd):
                continue
            score = result["paired"] - 0.5 * len(result["diffs"]) + (0.5 if pd.get("label") and pd.get("label") == rd.get("label") else 0.0)
            if best is None or score > best[0]:
                best = (score, pd_doc, pd, result)
        if identical:
            stats["rd_identical_to_pd"] += 1
            continue
        if best is None:
            stats["rd_without_pd_counterpart"] += 1
            continue
        _score, pd_doc, pd, result = best
        share = result["paired"] / max(len(pd["layers"]), len(rd["layers"]))
        for diff in result["diffs"]:
            pl = pd["layers"][diff["pd"]] if diff["pd"] is not None else None
            rl = rd["layers"][diff["rd"]] if diff["rd"] is not None else None
            kind = diff["kind"]
            if not all(material_like(layer["name"]) for layer in (pl, rl) if layer is not None):
                stats["layers_not_material_like"] = stats.get("layers_not_material_like", 0) + 1
                continue
            if kind in (KIND_REMOVED, KIND_MATERIAL) and pl is not None and _named_on_page(pl["name"], rd):
                stats["layers_named_elsewhere_on_other_page"] = stats.get("layers_named_elsewhere_on_other_page", 0) + 1
                continue
            if kind in (KIND_ADDED, KIND_MATERIAL) and rl is not None and _named_on_page(rl["name"], pd):
                stats["layers_named_elsewhere_on_other_page"] = stats.get("layers_named_elsewhere_on_other_page", 0) + 1
                continue
            label = rd.get("label") or pd.get("label")
            what = f"конструкции {label}" if label else "конструкции"
            if kind == KIND_REMOVED:
                text = f"В составе {what} в ПД есть слой «{_fmt(pl)}», в соответствующем составе РД такого слоя нет."
                location_part = pl["name"]
            elif kind == KIND_ADDED:
                text = f"В составе {what} в РД есть слой «{_fmt(rl)}», в соответствующем составе ПД такого слоя нет."
                location_part = rl["name"]
            elif kind == KIND_MATERIAL:
                text = f"В составе {what} на одном месте в ПД слой «{_fmt(pl)}», в РД — «{_fmt(rl)}» (материалы различаются по смыслу)."
                location_part = pl["name"]
            else:
                text = f"В составе {what} толщина слоя «{pl['name']}» в ПД {diff['pd_mm']} мм, в РД {diff['rd_mm']} мм."
                location_part = pl["name"]
            confidence = 0.5 + 0.3 * share - 0.07 * max(0, len(result["diffs"]) - 1) + (0.05 if label and pd.get("label") == rd.get("label") else 0.0)
            confidence = round(max(0.2, min(0.85, confidence)), 3)
            criticality = CRITICALITY_SUBSTANTIAL
            if kind == KIND_THICKNESS:
                a, b = Decimal(diff["pd_mm"]), Decimal(diff["rd_mm"])
                if a and abs(a - b) / a < Decimal("0.1"):
                    criticality = CRITICALITY_MINOR
            description = (f"{text} Параметр Матрицы не определён: состав слоёв сопоставлен по порядку слоёв и смыслу названий "
                           f"(совпало слоёв: {result['paired']} из {len(pd['layers'])} в ПД и {len(rd['layers'])} в РД); вывод за инспектором.")
            out.append(HypothesisCandidate(
                method=METHOD_LAYERS, detector=DETECTOR,
                dedup_key=make_dedup_key(object_id, METHOD_LAYERS, kind, pl["name"] if pl else "", rl["name"] if rl else "", diff.get("pd_mm") or (pl or {}).get("mm", ""),
                                         diff.get("rd_mm") or (rl or {}).get("mm", ""), label or ""),
                description=description, confidence=confidence, criticality=criticality,
                evidence=[_layer_ref(pd_doc, pd, pl, stage="PD", role="expected", value=_fmt(pl) if pl else "нет такого слоя в ПД", confidence=confidence),
                          _layer_ref(rd_doc, rd, rl, stage="RD", role="actual", value=_fmt(rl) if rl else "нет такого слоя в РД", confidence=confidence)],
                location=f"{label + ': ' if label else ''}{location_part}", expected=_fmt(pl) if pl else None, actual=_fmt(rl) if rl else None, normative_base=None,
                details={"rule_name": "Расхождение состава слоёв конструкции (ПД ↔ РД)", "layer_diff": kind, "pd_label": pd.get("label"), "rd_label": rd.get("label"),
                         "paired_layers": result["paired"], "pd_layers": len(pd["layers"]), "rd_layers": len(rd["layers"]), "diffs_in_construction": len(result["diffs"]),
                         "evidence_required": ["PD", "RD"]},
                editions=tuple(sorted({int(pd_doc.id), int(rd_doc.id)})),
            ))
    stats["hypotheses"] = len(out)
    return out, stats
