"""Glue of the sheet comparison to documents of one inspection process: document pairs, sheet pairs, one pair compared.

A *document pair* is formed only inside one discipline (the section tags of `document_facts`):
  stage      PD <-> RD of the same discipline;
  revision   two RD files of the same series (same section key and cipher family) that carry different revision markers
             ("изм. 3" vs "изм. 2"); the older revision is on the left.
Sheets are paired inside a document pair by `matching.match_sheets`; nothing here states a violation.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Iterable

from . import config as cfg
from . import matching, meta as sheet_meta, raster

logger = logging.getLogger(__name__)
MAX_DOCUMENT_PAIRS = 12
MAX_PAGES_PER_DOCUMENT = 700


def status() -> dict[str, Any]:
    return {"enabled": cfg.sheet_compare_enabled(), **raster.status(), "min_pair_confidence": cfg.MIN_PAIR_CONFIDENCE,
            "graphic_diff_min_pair_confidence": cfg.GRAPHIC_PAIR_CONFIDENCE}


def _stage(document: Any) -> str | None:
    from ..explication_compare import _doc_stage

    return _doc_stage(document)


def _bytes(document: Any) -> tuple[bytes, str]:
    from ..inspector_workbench import document_bytes

    data = document_bytes(document)
    return data, str(document.file_hash or document.content_hash or hashlib.sha256(data).hexdigest())


def document_pairs(documents: Iterable[Any], gate: Any, *, kinds: Iterable[str] = ("stage", "revision")) -> list[dict[str, Any]]:
    """Candidate document pairs of one object, inside one discipline, deterministic order."""
    from ..explication_compare import _revision_rank

    docs = [d for d in documents if getattr(d, "id", None) is not None and _stage(d) in ("PD", "RD")]
    facts = {int(d.id): gate.facts.get(int(d.id)) for d in docs}
    usable = [d for d in docs if facts[int(d.id)] is not None and facts[int(d.id)].tags and int(d.id) not in gate.excluded_doc_ids]
    out: list[dict[str, Any]] = []
    if "stage" in kinds:
        pd = [d for d in usable if _stage(d) == "PD"]
        rd = [d for d in usable if _stage(d) == "RD"]
        for left in pd:
            for right in rd:
                common = facts[int(left.id)].tags & facts[int(right.id)].tags
                if common:
                    out.append({"kind": "stage", "left": left, "right": right, "discipline": sorted(common)[0]})
    if "revision" in kinds:
        rd = [d for d in usable if _stage(d) == "RD"]
        for i, left in enumerate(rd):
            for right in rd[i + 1:]:
                fl, fr = facts[int(left.id)], facts[int(right.id)]
                if not fl.section_key or fl.section_key != fr.section_key or not fl.family or fl.family != fr.family or fl.building != fr.building:
                    continue
                if fl.sha256 and fl.sha256 == fr.sha256:
                    continue
                older, newer = (left, right) if _revision_rank(fl) <= _revision_rank(fr) else (right, left)
                out.append({"kind": "revision", "left": older, "right": newer, "discipline": fl.section_key})
    out.sort(key=lambda p: (p["kind"], int(p["left"].id), int(p["right"].id)))
    return out


def _scan(document: Any, max_pages: int) -> tuple[dict[int, dict[str, Any]], bool]:
    data, sha = _bytes(document)
    import fitz

    with fitz.open(stream=data, filetype="pdf") as pdf:
        total = len(pdf)
    pages = range(1, min(total, max_pages) + 1)
    return sheet_meta.document_meta(data, sha, pages=pages), total > max_pages


def _sheet_view(document: Any, page: int, meta: dict[str, Any]) -> dict[str, Any]:
    return {"document_id": int(document.id), "filename": document.filename, "stage": _stage(document), "page": page,
            "revision": document.revision, "sheet_no": meta.get("sheet_no"), "cipher": meta.get("cipher"), "title": meta.get("title"),
            "kind": meta.get("kind"), "width": meta.get("width"), "height": meta.get("height")}


def sheet_pairs(documents: list[Any], gate: Any, *, left_id: int | None = None, right_id: int | None = None, kinds: Iterable[str] = ("stage", "revision"),
                min_confidence: float | None = None, limit: int = 200, max_pages: int = MAX_PAGES_PER_DOCUMENT,
                max_document_pairs: int = MAX_DOCUMENT_PAIRS) -> dict[str, Any]:
    """Matched sheet pairs of the object. `left_id` / `right_id` restrict the document pairs; without them the first
    `max_document_pairs` document pairs are scanned and the rest is reported as `truncated_document_pairs`."""
    t0 = time.monotonic()
    pairs = document_pairs(documents, gate, kinds=kinds)
    if left_id is not None:
        pairs = [p for p in pairs if int(p["left"].id) == int(left_id)]
    if right_id is not None:
        pairs = [p for p in pairs if int(p["right"].id) == int(right_id)]
    truncated_pairs = max(0, len(pairs) - max_document_pairs)
    pairs = pairs[:max_document_pairs]
    metas: dict[int, dict[int, dict[str, Any]]] = {}
    truncated_docs: set[int] = set()
    result: list[dict[str, Any]] = []
    for item in pairs:
        for doc in (item["left"], item["right"]):
            if int(doc.id) not in metas:
                try:
                    metas[int(doc.id)], cut = _scan(doc, max_pages)
                except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
                    logger.info("sheet scan skipped for document %s: %s", doc.id, exc)
                    metas[int(doc.id)], cut = {}, False
                if cut:
                    truncated_docs.add(int(doc.id))
        left_items = [{"key": n, "meta": m} for n, m in metas[int(item["left"].id)].items() if sheet_meta.is_drawing(m)]
        right_items = [{"key": n, "meta": m} for n, m in metas[int(item["right"].id)].items() if sheet_meta.is_drawing(m)]
        for match in matching.match_sheets(left_items, right_items, same_series=item["kind"] == "revision", min_confidence=min_confidence):
            lm, rm = metas[int(item["left"].id)][match["left"]], metas[int(item["right"].id)][match["right"]]
            result.append({"kind": item["kind"], "discipline": item["discipline"], "left": _sheet_view(item["left"], match["left"], lm),
                           "right": _sheet_view(item["right"], match["right"], rm), "pair_confidence": match["confidence"],
                           "ambiguous": match["ambiguous"], "margin": match["margin"], "components": match["components"], "basis": match["basis"],
                           "title_method": match["title_method"]})
    result.sort(key=lambda p: (-p["pair_confidence"], p["left"]["document_id"], p["left"]["page"], p["right"]["document_id"], p["right"]["page"]))
    return {"pairs": result[:limit], "total": len(result), "document_pairs": len(pairs), "truncated_document_pairs": truncated_pairs,
            "truncated_documents": sorted(truncated_docs), "pages_scanned": sum(len(m) for m in metas.values()), "seconds": round(time.monotonic() - t0, 1),
            "note": "Уверенность сопоставления, процент сходства листов и величина отклонения — разные величины; здесь только первая."}


def compare_sheets(left_doc: Any, left_page: int, right_doc: Any, right_page: int, *, same_series: bool = False, want_overlay: bool = False
                   ) -> tuple[dict[str, Any], bytes | None]:
    """One pair: the pairing evidence (confidence, basis) and the raster comparison (similarity, regions), kept apart."""
    data_l, sha_l = _bytes(left_doc)
    data_r, sha_r = _bytes(right_doc)
    meta_l = sheet_meta.document_meta(data_l, sha_l, pages=[int(left_page)]).get(int(left_page))
    meta_r = sheet_meta.document_meta(data_r, sha_r, pages=[int(right_page)]).get(int(right_page))
    if meta_l is None or meta_r is None:
        raise ValueError("Page is outside the document")
    pairing = matching.score_pair(meta_l, meta_r, same_series=same_series)
    t0 = time.monotonic()
    picture, overlay = raster.compare_cached(data_l, sha_l, int(left_page), data_r, sha_r, int(right_page), want_overlay=want_overlay)
    return {
        "left": _sheet_view(left_doc, int(left_page), meta_l), "right": _sheet_view(right_doc, int(right_page), meta_r),
        "pairing": {"confidence": pairing["confidence"], "components": pairing["components"], "basis": pairing["basis"], "rejected": pairing["reject"],
                    "title_method": pairing.get("title_method")},
        "picture": picture, "seconds": round(time.monotonic() - t0, 2),
        "note": "Процент сходства — доля совпавших штрихов после выравнивания; уверенность сопоставления — отдельная величина; "
                "величина отклонения и нарушение здесь не определяются.",
    }, overlay
