"""GRAPHIC_DIFF detector: a significant region where the strokes of two paired sheets differ (ТЗ 9.1 CV part, ТЗ 9.5).

Not in DEFAULT_METHODS: it runs only when named in CASE10_FREE_HYPOTHESES_METHODS and CASE10_SHEET_COMPARE_ENABLED=1 (or
CASE10_GRAPHIC_DIFF_FORCE=1). A hypothesis is made only for
  * a sheet pair inside one discipline whose pair confidence is >= CASE10_GRAPHIC_DIFF_MIN_PAIR_CONFIDENCE and not ambiguous;
  * two sheets of one composition (picture similarity >= `raster.LAYOUT_SAME`), i.e. a difference that localises;
  * a region of at least CASE10_GRAPHIC_DIFF_MIN_REGION_MM2, at most CASE10_GRAPHIC_DIFF_MAX_PER_PAIR per pair and
    CASE10_GRAPHIC_DIFF_MAX_PER_DOCUMENT per document.
The score is a ranking value (pair confidence scaled by the region size), NOT a probability of a violation; the picture
similarity, the pair confidence and the (absent) deviation are written separately into the details.
"""
from __future__ import annotations

import os
from typing import Any

from ..sheet_compare import config as cfg
from .base import (
    CRITICALITY_MINOR,
    METHOD_GRAPHIC,
    EvidenceRef,
    HypothesisCandidate,
    make_dedup_key,
    norm_token,
)

DETECTOR = "graphic_diff.regions"
_KIND_TEXT = {
    "only_left": "штрихи есть только на листе ПД (на листе РД их нет)",
    "only_right": "штрихи есть только на листе РД (на листе ПД их нет)",
    "changed": "штрихи различаются на обоих листах",
}


def _kinds() -> tuple[str, ...]:
    raw = os.getenv("CASE10_GRAPHIC_DIFF_KINDS", "stage").strip()
    return tuple(k for k in (p.strip() for p in raw.split(",")) if k in ("stage", "revision")) or ("stage",)


def detect(documents: list[Any], gate: Any, *, object_id: str | None) -> tuple[list[HypothesisCandidate], dict[str, Any]]:
    from ..sheet_compare import raster, service

    stats: dict[str, Any] = {"enabled": cfg.graphic_diff_enabled(), "opencv": raster.available()}
    if not cfg.graphic_diff_enabled() or not raster.available():
        stats["skipped"] = "sheet_compare_disabled" if not cfg.graphic_diff_enabled() else "opencv_missing"
        return [], stats
    listing = service.sheet_pairs(list(documents), gate, kinds=_kinds(), min_confidence=cfg.GRAPHIC_PAIR_CONFIDENCE, limit=10_000,
                                 max_document_pairs=cfg.GRAPHIC_MAX_DOCUMENT_PAIRS)
    docs = {int(d.id): d for d in documents}
    stats.update({"pairs_matched": listing["total"], "document_pairs": listing["document_pairs"], "truncated_document_pairs": listing["truncated_document_pairs"],
                  "pages_scanned": listing.get("pages_scanned")})
    eligible = [p for p in listing["pairs"] if not p["ambiguous"]]
    stats["pairs_ambiguous"] = listing["total"] - len(eligible)
    eligible = eligible[:cfg.GRAPHIC_MAX_PAIRS]
    stats.update({"pairs_compared": len(eligible), "different_layout": 0, "not_comparable": 0, "identical": 0, "with_regions": 0})
    out: list[HypothesisCandidate] = []
    per_document: dict[int, int] = {}
    for pair in eligible:
        left_doc, right_doc = docs[pair["left"]["document_id"]], docs[pair["right"]["document_id"]]
        try:
            result, _ = service.compare_sheets(left_doc, pair["left"]["page"], right_doc, pair["right"]["page"], same_series=pair["kind"] == "revision")
        except (FileNotFoundError, ValueError, RuntimeError, OSError):
            stats["not_comparable"] += 1
            continue
        picture = result["picture"]
        if not picture.get("comparable"):
            stats["not_comparable"] += 1
            continue
        if picture.get("layout_match") != "same":
            stats["different_layout"] += 1
            continue
        similarity = picture.get("similarity") or 0.0
        if similarity > cfg.GRAPHIC_MAX_SIMILARITY:
            stats["identical"] += 1
            continue
        regions = [r for r in picture.get("regions") or [] if r["area_mm2"] >= cfg.GRAPHIC_MIN_REGION_MM2][:cfg.GRAPHIC_MAX_PER_PAIR]
        if regions:
            stats["with_regions"] += 1
        for region in regions:
            if per_document.get(int(right_doc.id), 0) >= cfg.GRAPHIC_MAX_PER_DOCUMENT:
                stats["cut_by_document_limit"] = stats.get("cut_by_document_limit", 0) + 1
                continue
            per_document[int(right_doc.id)] = per_document.get(int(right_doc.id), 0) + 1
            out.append(_candidate(pair, picture, region, left_doc, right_doc, object_id))
    stats["hypotheses"] = len(out)
    return out, stats


def _candidate(pair: dict[str, Any], picture: dict[str, Any], region: dict[str, Any], left_doc: Any, right_doc: Any, object_id: str | None) -> HypothesisCandidate:
    left, right = pair["left"], pair["right"]
    wl, hl = picture["left_size_pt"]
    wr, hr = picture["right_size_pt"]
    x0, y0, x1, y1 = region["bbox_left"]
    title = right.get("title") or left.get("title") or "лист"
    confidence = round(max(0.2, min(0.85, pair["pair_confidence"] * (0.5 + 0.5 * min(1.0, region["area_mm2"] / 5000.0)))), 3)
    left_stage = "PD" if pair["kind"] == "stage" else "RD"
    left_label = "ПД" if pair["kind"] == "stage" else "РД (предыдущая редакция)"
    right_label = "РД" if pair["kind"] == "stage" else "РД (новая редакция)"
    description = (
        f"Листы «{title}» ({left_label}, стр. {left['page']}; {right_label}, стр. {right['page']}) сопоставлены по названию и содержанию "
        f"(уверенность сопоставления {pair['pair_confidence']:.2f}); процент сходства листов {100 * (picture.get('similarity') or 0):.1f} %. "
        f"В области площадью около {region['area_mm2'] / 100:.0f} см² {_KIND_TEXT[region['kind']]}. "
        f"Растровое сравнение показывает только, что рисунок различается: чем это вызвано и является ли это нарушением — решает инспектор."
    )
    centre = ((x0 + x1) / 2 / wl, (y0 + y1) / 2 / hl)
    return HypothesisCandidate(
        method=METHOD_GRAPHIC, detector=DETECTOR,
        dedup_key=make_dedup_key(object_id, METHOD_GRAPHIC, pair["kind"], norm_token(title), left["page"], right["page"], round(centre[0] * 20), round(centre[1] * 20)),
        description=description, confidence=confidence, criticality=CRITICALITY_MINOR,
        evidence=[
            EvidenceRef(document=left_doc, page=int(left["page"]), stage=left_stage, role="expected", bbox_pdf=[float(v) for v in region["bbox_left"]],
                        page_width=wl, page_height=hl, value="штрихи листа", context=f"{left.get('title') or ''}".strip()[:300] or None, confidence=pair["pair_confidence"]),
            EvidenceRef(document=right_doc, page=int(right["page"]), stage="RD", role="actual", bbox_pdf=[float(v) for v in region["bbox_right"]],
                        page_width=wr, page_height=hr, value="штрихи листа", context=f"{right.get('title') or ''}".strip()[:300] or None, confidence=pair["pair_confidence"]),
        ],
        location=f"{title}; область {centre[0] * 100:.0f} %, {centre[1] * 100:.0f} % листа",
        expected=None, actual=None, normative_base=None,
        details={"rule_name": "Различие рисунка сопоставленных листов (растровое сравнение)", "pair_kind": pair["kind"], "discipline": pair["discipline"],
                 "pair_confidence": pair["pair_confidence"], "pair_components": pair["components"], "picture_similarity": picture.get("similarity"),
                 "region_kind": region["kind"], "region_area_mm2": region["area_mm2"], "alignment": (picture.get("alignment") or {}).get("method"),
                 "deviation": None, "evidence_required": ["PD", "RD"]},
        editions=tuple(sorted({int(left_doc.id), int(right_doc.id)})),
    )
