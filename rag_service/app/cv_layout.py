from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class CVRegion:
    region_type: str
    bbox: List[int]  # [x0,y0,x1,y1] in px
    score: float = 0.0
    source: str = "opencv"


def _try_import_cv():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        return cv2, np
    except Exception:
        return None, None


def _clamp_bbox(b: List[int], w: int, h: int) -> List[int]:
    return [max(0, b[0]), max(0, b[1]), min(w, b[2]), min(h, b[3])]


def _bbox_area(b: List[int]) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _intersects(a: List[int], b: List[int]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _mask_regions(img_shape, bboxes: List[List[int]]):
    cv2, np = _try_import_cv()
    if cv2 is None:
        return None
    mask = np.zeros(img_shape[:2], dtype=np.uint8)
    for b in bboxes:
        x0, y0, x1, y1 = b
        x0 = max(0, int(x0)); y0 = max(0, int(y0)); x1 = max(0, int(x1)); y1 = max(0, int(y1))
        cv2.rectangle(mask, (x0, y0), (x1, y1), 255, thickness=-1)
    return mask


def detect_tables_cv(
    img: Image.Image,
    *,
    exclude_bboxes: Optional[List[List[int]]] = None,
    max_tables: int = 12,
) -> List[CVRegion]:
    """
    Robust table detection on CAD sheets.

    Key idea: tables have *grid-like* structure:
      - strong horizontal AND vertical line evidence
      - many intersections (junctions)

    We use 2 kernel scales to handle both dense and sparse grids.
    We also apply strong sanity checks to avoid "whole page == table".
    """
    cv2, np = _try_import_cv()
    if cv2 is None:
        logger.warning("OpenCV not available; detect_tables_cv disabled.")
        return []

    w, h = img.size
    page_area = max(1, w * h)

    gray = np.array(img.convert("L"))

    # mask excludes (title block etc.) to reduce false positives
    if exclude_bboxes:
        m = _mask_regions(gray.shape, exclude_bboxes)
        if m is not None:
            gray = gray.copy()
            gray[m == 255] = 255

    thr = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 7
    )

    # Two scales: small & medium kernels
    def extract_lines(kernel_factor: int):
        hk_len = max(18, w // kernel_factor)
        vk_len = max(18, h // kernel_factor)
        hk = cv2.getStructuringElement(cv2.MORPH_RECT, (hk_len, 1))
        vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk_len))
        horiz = cv2.dilate(cv2.erode(thr, hk, 1), hk, 2)
        vert = cv2.dilate(cv2.erode(thr, vk, 1), vk, 2)
        inter = cv2.bitwise_and(horiz, vert)
        grid = cv2.bitwise_or(horiz, vert)
        return horiz, vert, inter, grid

    all_candidates: List[CVRegion] = []
    for kf in (55, 35):  # smaller kernel => more sensitive; medium => more stable
        horiz, vert, inter, grid = extract_lines(kf)
        grid = cv2.dilate(grid, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), 1)

        contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            if bw < w * 0.10 or bh < h * 0.05:
                continue
            area = bw * bh
            # reject huge regions early
            if area > page_area * 0.60:
                continue

            sub_h = horiz[y:y + bh, x:x + bw]
            sub_v = vert[y:y + bh, x:x + bw]
            sub_i = inter[y:y + bh, x:x + bw]

            h_cnt = cv2.countNonZero(sub_h)
            v_cnt = cv2.countNonZero(sub_v)
            i_cnt = cv2.countNonZero(sub_i)

            # normalize
            h_ratio = h_cnt / max(1, area)
            v_ratio = v_cnt / max(1, area)
            i_ratio = i_cnt / max(1, area)

            # Require both line directions (but relax slightly for dense sheets)
            if h_ratio < 0.0015 or v_ratio < 0.0015:
                continue
            # Require intersections (junction richness)
            if i_ratio < 0.00008:
                continue

            # Avoid the page border rectangle
            margin = int(min(w, h) * 0.012)
            if x <= margin and y <= margin and (x + bw) >= (w - margin) and (y + bh) >= (h - margin):
                continue

            # Penalize extremely elongated regions (often frames / separators)
            ar = (bw / max(1, bh)) if bh else 999.0
            if ar > 10.0 or ar < 0.10:
                continue

            score = (h_ratio + v_ratio) * 0.5 + min(0.02, i_ratio * 12.0)
            all_candidates.append(CVRegion(region_type="table", bbox=[x, y, x + bw, y + bh], score=score, source=f"opencv_k{kf}"))

    # sort + NMS
    all_candidates.sort(key=lambda r: r.score, reverse=True)
    kept: List[CVRegion] = []
    for r in all_candidates:
        if len(kept) >= max_tables:
            break
        ra = _bbox_area(r.bbox)
        dup = False
        for k in kept:
            ix0 = max(r.bbox[0], k.bbox[0])
            iy0 = max(r.bbox[1], k.bbox[1])
            ix1 = min(r.bbox[2], k.bbox[2])
            iy1 = min(r.bbox[3], k.bbox[3])
            inter_a = max(0, ix1 - ix0) * max(0, iy1 - iy0)
            if inter_a / max(1, ra) > 0.70:
                dup = True
                break
        if not dup:
            kept.append(r)

    return kept


def _ink_mask(img: Image.Image, *, exclude_bboxes: Optional[List[List[int]]] = None):
    cv2, np = _try_import_cv()
    if cv2 is None:
        return None
    gray = np.array(img.convert("L"))

    if exclude_bboxes:
        m = _mask_regions(gray.shape, exclude_bboxes)
        if m is not None:
            gray = gray.copy()
            gray[m == 255] = 255

    thr = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 31, 7)
    # connect linework (conservative to avoid gluing many separate views)
    w, h = img.size
    k = max(2, w // 900)
    thr = cv2.morphologyEx(thr, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)), iterations=1)
    thr = cv2.dilate(thr, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)), iterations=1)
    thr = cv2.morphologyEx(thr, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k*2, k*2)), iterations=1)
    return thr


def _find_gutter_splits(mask, bbox: List[int]):
    """Find strong whitespace gutters inside bbox for potential splits (vertical/horizontal)."""
    cv2, np = _try_import_cv()
    if cv2 is None:
        return [], []

    x0, y0, x1, y1 = bbox
    roi = mask[y0:y1, x0:x1]
    if roi.size == 0:
        return [], []

    # ink density projections
    col = roi.sum(axis=0)  # higher => more ink
    row = roi.sum(axis=1)

    # convert to "ink fraction" per column/row
    # mask is 0/255; sum max is 255*height
    h = roi.shape[0]; w = roi.shape[1]
    col_frac = col / max(1, 255 * h)
    row_frac = row / max(1, 255 * w)

    # gutter is where ink fraction is very low for a run
    v_thresh = 0.02
    h_thresh = 0.02
    min_run = max(6, int(w * 0.03))
    min_run_h = max(6, int(h * 0.03))

    v_splits = []
    run_start = None
    for i, f in enumerate(col_frac):
        if f <= v_thresh and run_start is None:
            run_start = i
        if f > v_thresh and run_start is not None:
            if i - run_start >= min_run:
                v_splits.append((run_start, i))
            run_start = None
    if run_start is not None and w - run_start >= min_run:
        v_splits.append((run_start, w))

    h_splits = []
    run_start = None
    for i, f in enumerate(row_frac):
        if f <= h_thresh and run_start is None:
            run_start = i
        if f > h_thresh and run_start is not None:
            if i - run_start >= min_run_h:
                h_splits.append((run_start, i))
            run_start = None
    if run_start is not None and h - run_start >= min_run_h:
        h_splits.append((run_start, h))

    # return midpoints in page coords
    v_cuts = [x0 + (a + b)//2 for a, b in v_splits if 0.15*w < (a+b)//2 < 0.85*w]
    h_cuts = [y0 + (a + b)//2 for a, b in h_splits if 0.15*h < (a+b)//2 < 0.85*h]
    return v_cuts, h_cuts


def detect_drawings_cv(
    img: Image.Image,
    *,
    exclude_bboxes: Optional[List[List[int]]] = None,
    max_drawings: int = 10,
) -> List[CVRegion]:
    """
    Detect drawings as large linework clusters after masking out text/title/tables.

    Problem on CAD sheets: many views are connected by thin dimension/leader lines, so naive
    connected-components yields a huge bbox. We handle this by:
      1) conservative morphology (less "glue")
      2) if a component is still huge -> split by whitespace gutters (multiple cuts, recursive)
    """
    cv2, np = _try_import_cv()
    if cv2 is None:
        logger.warning("OpenCV not available; detect_drawings_cv disabled.")
        return []

    w, h = img.size
    page_area = max(1, w * h)

    mask = _ink_mask(img, exclude_bboxes=exclude_bboxes)
    if mask is None:
        return []

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: List[List[int]] = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        area = bw * bh
        if area < page_area * 0.02:
            continue
        if bw < w * 0.12 or bh < h * 0.10:
            continue
        boxes.append([x, y, x + bw, y + bh])

    if not boxes:
        return []

    # merge only clearly overlapping boxes (avoid over-merge that creates half-page bbox)
    def iou(a: List[int], b: List[int]) -> float:
        ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1])
        ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
        inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
        if inter <= 0:
            return 0.0
        ua = _bbox_area(a) + _bbox_area(b) - inter
        return inter / max(1, ua)

    merged: List[List[int]] = []
    for b in sorted(boxes, key=_bbox_area, reverse=True):
        placed = False
        for mb in merged:
            if iou(b, mb) > 0.10:  # only overlap-based merge
                mb[0] = min(mb[0], b[0]); mb[1] = min(mb[1], b[1]); mb[2] = max(mb[2], b[2]); mb[3] = max(mb[3], b[3])
                placed = True
                break
        if not placed:
            merged.append(b)

    # recursive gutter split for huge boxes
    def split_box(b: List[int], depth: int = 0) -> List[List[int]]:
        if depth >= 2:
            return [b]
        if _bbox_area(b) <= page_area * 0.30:
            return [b]
        v_cuts, h_cuts = _find_gutter_splits(mask, b)
        pieces: List[List[int]] = []

        # choose best cut: one that yields two reasonably big parts
        best = None
        best_gain = 0.0

        for cut in v_cuts[:3]:
            left = [b[0], b[1], cut, b[3]]
            right = [cut, b[1], b[2], b[3]]
            a1, a2 = _bbox_area(left), _bbox_area(right)
            if a1 > page_area * 0.06 and a2 > page_area * 0.06:
                gain = 1.0 - (max(a1, a2) / max(1, _bbox_area(b)))
                if gain > best_gain:
                    best_gain = gain
                    best = ("v", cut)

        for cut in h_cuts[:3]:
            top = [b[0], b[1], b[2], cut]
            bot = [b[0], cut, b[2], b[3]]
            a1, a2 = _bbox_area(top), _bbox_area(bot)
            if a1 > page_area * 0.06 and a2 > page_area * 0.06:
                gain = 1.0 - (max(a1, a2) / max(1, _bbox_area(b)))
                if gain > best_gain:
                    best_gain = gain
                    best = ("h", cut)

        if not best:
            return [b]

        kind, cut = best
        if kind == "v":
            parts = [[b[0], b[1], cut, b[3]], [cut, b[1], b[2], b[3]]]
        else:
            parts = [[b[0], b[1], b[2], cut], [b[0], cut, b[2], b[3]]]

        for p in parts:
            pieces.extend(split_box(p, depth + 1))
        return pieces

    final_boxes: List[List[int]] = []
    for b in merged:
        final_boxes.extend(split_box(b, 0))

    # filter + sort
    out: List[CVRegion] = []
    for b in final_boxes:
        area = _bbox_area(b)
        if area < page_area * 0.04:
            continue
        # reject near-full page
        if area > page_area * 0.80:
            continue
        out.append(CVRegion(region_type="drawing", bbox=_clamp_bbox(b, w, h), score=area / page_area))

    out.sort(key=lambda r: r.score, reverse=True)
    return out[:max_drawings]




def table_grid_score(img: Image.Image, bbox: List[int]) -> float:
    """Lightweight validation score for a table bbox (0..1-ish)."""
    cv2, np = _try_import_cv()
    if cv2 is None:
        return 0.0
    w, h = img.size
    x0, y0, x1, y1 = _clamp_bbox(bbox, w, h)
    roi = np.array(img.crop((x0, y0, x1, y1)).convert("L"))
    if roi.size == 0:
        return 0.0
    thr = cv2.adaptiveThreshold(roi, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 31, 7)
    bw, bh = roi.shape[1], roi.shape[0]
    area = max(1, bw * bh)
    hk_len = max(12, bw // 25)
    vk_len = max(12, bh // 25)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (hk_len, 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk_len))
    horiz = cv2.dilate(cv2.erode(thr, hk, 1), hk, 2)
    vert = cv2.dilate(cv2.erode(thr, vk, 1), vk, 2)
    inter = cv2.bitwise_and(horiz, vert)
    h_ratio = cv2.countNonZero(horiz) / area
    v_ratio = cv2.countNonZero(vert) / area
    i_ratio = cv2.countNonZero(inter) / area
    # combine
    return float(min(1.0, (h_ratio + v_ratio) * 40.0 + i_ratio * 400.0))


def ink_density(img: Image.Image, bbox: List[int], *, exclude_bboxes: Optional[List[List[int]]] = None) -> float:
    """Ink density inside bbox after excluding regions. 0..1."""
    cv2, np = _try_import_cv()
    if cv2 is None:
        return 0.0
    w, h = img.size
    x0, y0, x1, y1 = _clamp_bbox(bbox, w, h)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    gray = np.array(img.convert("L"))
    if exclude_bboxes:
        m = _mask_regions(gray.shape, exclude_bboxes)
        if m is not None:
            gray = gray.copy()
            gray[m == 255] = 255
    roi = gray[y0:y1, x0:x1]
    thr = cv2.adaptiveThreshold(roi, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 31, 7)
    return float(cv2.countNonZero(thr) / max(1, thr.size))


def split_drawing_grid_candidates(
    img: Image.Image,
    bbox: List[int],
    *,
    rows: int = 2,
    cols: int = 2,
    exclude_bboxes: Optional[List[List[int]]] = None,
    min_density: float = 0.02,
) -> List[CVRegion]:
    """
    If we can't separate drawings reliably, generate grid candidates inside bbox and
    keep tiles that have enough ink. This is a "radical" fallback that at least tries.
    """
    w, h = img.size
    x0, y0, x1, y1 = _clamp_bbox(bbox, w, h)
    bw = max(1, x1 - x0); bh = max(1, y1 - y0)
    out: List[CVRegion] = []
    for r in range(rows):
        for c in range(cols):
            tx0 = x0 + int(bw * c / cols)
            tx1 = x0 + int(bw * (c + 1) / cols)
            ty0 = y0 + int(bh * r / rows)
            ty1 = y0 + int(bh * (r + 1) / rows)
            tb = [tx0, ty0, tx1, ty1]
            dens = ink_density(img, tb, exclude_bboxes=exclude_bboxes)
            if dens >= min_density:
                out.append(CVRegion(region_type="drawing", bbox=tb, score=dens, source="grid_fallback"))
    out.sort(key=lambda x: x.score, reverse=True)
    return out
