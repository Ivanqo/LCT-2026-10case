# app/titleblock_detector.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class TitleBlockConfig:
    # Search zone in bottom-right
    zone_w_frac: float = 0.40   # right 40% of width
    zone_h_frac: float = 0.30   # bottom 30% of height
    pad: int = 12               # expand bbox in pixels

    # Sanity constraints (relative to whole page)
    min_w_frac: float = 0.18
    min_h_frac: float = 0.10
    max_area_frac: float = 0.30  # do not allow tb bbox larger than 30% of page area


def _clamp_bbox(b: List[int], w: int, h: int) -> List[int]:
    return [max(0, b[0]), max(0, b[1]), min(w, b[2]), min(h, b[3])]


def _bbox_area(b: List[int]) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _intersects(a: List[int], b: List[int]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def detect_title_block_bbox(
    *,
    page_w: int,
    page_h: int,
    words: List[Tuple[float, float, float, float, str]],
    page_image_bgr=None,
    cfg: TitleBlockConfig = TitleBlockConfig(),
) -> Optional[List[int]]:
    """
    Returns bbox [x0,y0,x1,y1] in pixel coords.
    Strategy:
      1) Restrict to bottom-right zone.
      2) If words exist inside zone -> tight bbox around those words (+pad), then clamp to zone.
      3) If no words and OpenCV image available -> try to find a rectangular frame inside zone (stamp box).
      4) Fallback to zone itself.
    """

    # Define bottom-right zone
    zx0 = int(page_w * (1.0 - cfg.zone_w_frac))
    zy0 = int(page_h * (1.0 - cfg.zone_h_frac))
    zone = [zx0, zy0, page_w, page_h]

    # 1) Word-based refine
    inside = []
    for x0, y0, x1, y1, t in words:
        if not t or not str(t).strip():
            continue
        wb = [int(x0), int(y0), int(x1), int(y1)]
        if _intersects(wb, zone):
            inside.append(wb)

    if inside:
        x0 = min(b[0] for b in inside) - cfg.pad
        y0 = min(b[1] for b in inside) - cfg.pad
        x1 = max(b[2] for b in inside) + cfg.pad
        y1 = max(b[3] for b in inside) + cfg.pad
        b = _clamp_bbox([x0, y0, x1, y1], page_w, page_h)
        # clamp to bottom-right zone strictly
        b = [max(b[0], zone[0]), max(b[1], zone[1]), min(b[2], zone[2]), min(b[3], zone[3])]

        # Sanity
        if (b[2] - b[0]) < page_w * cfg.min_w_frac or (b[3] - b[1]) < page_h * cfg.min_h_frac:
            return None
        if _bbox_area(b) > int(page_w * page_h * cfg.max_area_frac):
            # too large -> fallback to zone (still safe)
            return zone
        return b

    # 2) Contour-based fallback (only if cv2 available and image provided)
    if page_image_bgr is not None:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore

            img = page_image_bgr
            x0, y0, x1, y1 = zone
            roi = img[y0:y1, x0:x1]

            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            # emphasize lines
            thr = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                        cv2.THRESH_BINARY_INV, 31, 5)
            # line-ish morphology
            k = max(20, roi.shape[1] // 40)
            hk = cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1))
            vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, roi.shape[0] // 40)))
            h = cv2.dilate(cv2.erode(thr, hk, 1), hk, 2)
            v = cv2.dilate(cv2.erode(thr, vk, 1), vk, 2)
            grid = cv2.bitwise_or(h, v)
            grid = cv2.dilate(grid, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), 1)

            contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            best = None
            best_area = 0
            for c in contours:
                rx, ry, rw, rh = cv2.boundingRect(c)
                area = rw * rh
                if rw < roi.shape[1] * 0.35 or rh < roi.shape[0] * 0.25:
                    continue
                if area > best_area:
                    best_area = area
                    best = (rx, ry, rw, rh)

            if best is not None:
                rx, ry, rw, rh = best
                b = [x0 + rx - cfg.pad, y0 + ry - cfg.pad, x0 + rx + rw + cfg.pad, y0 + ry + rh + cfg.pad]
                b = _clamp_bbox(b, page_w, page_h)
                # sanity clamp + constraints
                if (b[2] - b[0]) < page_w * cfg.min_w_frac or (b[3] - b[1]) < page_h * cfg.min_h_frac:
                    return zone
                if _bbox_area(b) > int(page_w * page_h * cfg.max_area_frac):
                    return zone
                return b
        except Exception:
            pass

    # 3) Fallback to zone (safe, bounded)
    return zone
