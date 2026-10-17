"""Reference (pre-optimisation) page-quality check, kept for equivalence tests only.

This is the `official_evidence._quality_for_page` body as of f0753c9/0e3c21e, with two additions that never change the
decision: an optional wall-clock deadline (`ReferenceTimeout`) and `dedupe=True`, which (a) computes the page's
`get_image_info(hashes=True)` once instead of once per xref and keeps `get_image_rects`' exact digest matching
(`Pixmap(xref).digest == info["digest"]`), and (b) removes duplicate image rectangles before the image x word product
(the predicate is identical, duplicates cannot change it).
"""
from __future__ import annotations

import time


class ReferenceTimeout(Exception):
    pass


def reference_quality(pdf, page_number: int, *, dedupe: bool = False, deadline_s: float | None = None,
                      ocr_words=None) -> dict:
    """`pdf`: open fitz.Document. `ocr_words(page_number) -> list|None` replaces the OCR fallback; raising means
    OCR unavailable (same as the original `except Exception`)."""
    import fitz
    from app.domain import dataset_sources

    started = time.monotonic()

    def check() -> None:
        if deadline_s is not None and time.monotonic() - started > deadline_s:
            raise ReferenceTimeout()

    reasons: list[str] = []
    result: dict = {"page": int(page_number), "rotation": 0, "ocr_confidence": None, "reasons": reasons}
    try:
        if page_number > len(pdf):
            result["quality_status"] = "LOW_QUALITY"
            result["reasons"] = ["PAGE_OUT_OF_RANGE"]
        else:
            page = pdf[page_number - 1]
            rotation = int(page.rotation) % 360
            result["rotation"] = rotation
            if rotation:
                reasons.append("ROTATED_PAGE")

            text = str(page.get_text("text") or "").strip()
            words = page.get_text("words") or []
            broken_text_layer = dataset_sources._looks_like_broken_cmap_text(text)
            if broken_text_layer or (not text and not words):
                try:
                    found = ocr_words(int(page_number))
                except Exception:
                    found = None
                confidences = [float(word.get("confidence")) for word in found
                               if isinstance(word.get("confidence"), (int, float))] if found else []
                mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
                result["ocr_confidence"] = round(mean_confidence, 1)
                if not confidences:
                    reasons.append("BROKEN_TEXT_LAYER_OCR_UNAVAILABLE" if broken_text_layer else "NO_TEXT_LAYER_OCR_UNAVAILABLE")
                elif mean_confidence < 55.0:
                    reasons.append("BROKEN_TEXT_LAYER_LOW_OCR_CONFIDENCE" if broken_text_layer else "NO_TEXT_LAYER_LOW_OCR_CONFIDENCE")

            word_rects = [fitz.Rect(word[:4]) for word in words if len(word) >= 5 and str(word[4]).strip()]
            for annotation in page.annots() or ():
                check()
                annotation_name = str((annotation.type or (None, ""))[1]).casefold()
                if "stamp" not in annotation_name:
                    continue
                if any(annotation.rect.intersects(rect) for rect in word_rects):
                    reasons.append("STAMP_OVER_TEXT")
                    break

            page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
            infos = None
            seen_rects: set = set()  # dedupe mode: a rectangle's verdict is independent of the xref that listed it
            if word_rects:
                for image in page.get_images(full=True):
                    check()
                    xref = int(image[0])
                    if dedupe:
                        if infos is None:
                            infos = page.get_image_info(hashes=True)
                        digest = fitz.Pixmap(page.parent, xref).digest
                        image_rects = [fitz.Rect(im["bbox"]) for im in infos if im["digest"] == digest]
                    else:
                        image_rects = page.get_image_rects(xref)
                    check()
                    if dedupe:
                        fresh = {(r.x0, r.y0, r.x1, r.y1) for r in image_rects} - seen_rects
                        seen_rects |= fresh
                        image_rects = [fitz.Rect(*coords) for coords in sorted(fresh)]
                    for image_rect in image_rects:
                        check()
                        if image_rect.is_empty or image_rect.get_area() / page_area >= 0.35:
                            continue
                        overlaps = 0
                        for word_rect in word_rects:
                            intersection = image_rect & word_rect
                            if not intersection.is_empty and intersection.get_area() / max(1.0, word_rect.get_area()) >= 0.3:
                                overlaps += 1
                        if overlaps >= 3:
                            reasons.append("IMAGE_STAMP_OVER_TEXT")
                            break
                    if "IMAGE_STAMP_OVER_TEXT" in reasons:
                        break
            result["quality_status"] = "LOW_QUALITY" if reasons else "OK"
            result["reasons"] = sorted(set(reasons))
    except ReferenceTimeout:
        raise
    except Exception as exc:
        result["quality_status"] = "LOW_QUALITY"
        result["reasons"] = [f"PAGE_QUALITY_READ_FAILED:{type(exc).__name__}"]
    return result
