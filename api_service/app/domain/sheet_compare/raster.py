"""Raster comparison of two sheets: alignment, picture similarity, difference regions, overlay.

Needs OpenCV (`cv2`); without it `available()` is False and every caller switches the feature off without failing.

Pipeline (all deterministic: fixed working size, fixed ORB/ECC settings, a fixed RNG seed, candidate transforms compared by a
score and tie-broken in a fixed order):
  1. both pages are rendered with PyMuPDF (visible frame, native rotation) onto the SAME canvas -- the long side is at most
     `MAX_RENDER_SIDE` pixels, at most `MAX_DPI` for the larger sheet; sheets whose aspect ratios differ by more than
     `ASPECT_TOLERANCE` are not rastered together (a different format / orientation is not a picture difference);
  2. the ink (dark strokes) is cut out; the frame and the title block (ГОСТ Р 21.101: 185 mm wide, up to ~75 mm high with the
     change table) are masked out of both sheets;
  3. the right sheet is aligned to the left one: candidates are the identity, an ORB + RANSAC similarity transform and an ECC affine
     refinement; the candidate with the best coincidence score wins (the identity wins ties);
  4. strokes within `TOLERANCE_MM` of a stroke of the other sheet coincide; the rest is "only on the left" / "only on the right";
     the similarity is the Dice share of coinciding strokes, the regions are connected clusters of the differing strokes with boxes in
     PDF points of each sheet.
The similarity measures the picture only; it is not a probability of anything and not the size of a deviation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config as cfg
from .meta import cache_dir

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depends on the environment
    import cv2
    import numpy as np
except Exception:  # noqa: BLE001
    cv2 = None
    np = None

INK_DARKNESS = 45          # 0..255 below white that still counts as a stroke
STAMP_WIDTH_MM = 190.0
STAMP_HEIGHT_MM = 75.0
FRAME_LEFT_MM = 22.0
FRAME_OTHER_MM = 7.0
SEED = 20261019
LAYOUT_SAME = 0.60         # picture similarity from which two sheets are one composition (the differences localise)
LAYOUT_PARTIAL = 0.35
DIFFUSE_AREA_SHARE = 0.35


def available() -> bool:
    return cv2 is not None


def status() -> dict[str, Any]:
    return {"available": available(), "opencv": getattr(cv2, "__version__", None), "model_version": cfg.MODEL_VERSION}


def _fingerprint() -> str:
    digest = hashlib.sha256(Path(__file__).read_bytes())
    digest.update(repr((cfg.MODEL_VERSION, cfg.MAX_RENDER_SIDE, cfg.MAX_DPI, cfg.TOLERANCE_MM, cfg.MIN_REGION_MM2, cfg.MAX_REGIONS,
                        getattr(cv2, "__version__", None))).encode())
    return digest.hexdigest()[:12]


_FP: str | None = None


def fingerprint() -> str:
    global _FP
    if _FP is None:
        _FP = _fingerprint()
    return _FP


# ---------------------------------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------------------------------
def _page_size(pdf_bytes: bytes, page_no: int) -> tuple[float, float]:
    import fitz

    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if page_no < 1 or page_no > len(pdf):
            raise ValueError("Page is outside the document")
        rect = pdf[page_no - 1].rect
        return float(rect.width), float(rect.height)


def _render_gray(pdf_bytes: bytes, page_no: int, size: tuple[int, int]) -> "np.ndarray":
    import fitz

    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        page = pdf[page_no - 1]
        rect = page.rect
        matrix = fitz.Matrix(size[0] / rect.width, size[1] / rect.height)
        pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csGRAY, alpha=False, annots=False)
        array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
    if array.shape != (size[1], size[0]):
        array = cv2.resize(array, size, interpolation=cv2.INTER_AREA)
    return array


def _ink(gray: "np.ndarray") -> "np.ndarray":
    return (gray < 255 - INK_DARKNESS).astype(np.uint8)


def _valid_mask(size: tuple[int, int], width_pt: float, height_pt: float) -> "np.ndarray":
    """1 where the drawing area is: outside the frame and the title block (millimetres of the left sheet)."""
    w, h = size
    pt_per_mm = 72.0 / 25.4
    mask = np.ones((h, w), np.uint8)
    px_per_mm_x = w / (width_pt / pt_per_mm)
    px_per_mm_y = h / (height_pt / pt_per_mm)
    left = int(round(FRAME_LEFT_MM * px_per_mm_x)) if width_pt / pt_per_mm > 250 else int(round(FRAME_OTHER_MM * px_per_mm_x * 2))
    other_x, other_y = int(round(FRAME_OTHER_MM * px_per_mm_x)), int(round(FRAME_OTHER_MM * px_per_mm_y))
    mask[:, :left] = 0
    mask[:, w - other_x:] = 0
    mask[:other_y, :] = 0
    mask[h - other_y:, :] = 0
    stamp_w = min(STAMP_WIDTH_MM * px_per_mm_x, 0.62 * w)
    stamp_h = min(STAMP_HEIGHT_MM * px_per_mm_y, 0.40 * h)
    mask[int(h - stamp_h):, int(w - stamp_w):] = 0
    return mask


# ---------------------------------------------------------------------------------------------------------------
# alignment
# ---------------------------------------------------------------------------------------------------------------
def _tolerance_px(size: tuple[int, int], width_pt: float) -> int:
    px_per_mm = size[0] / (width_pt * 25.4 / 72.0)
    return max(2, int(round(cfg.TOLERANCE_MM * px_per_mm)))


def _dilate(mask: "np.ndarray", radius: int) -> "np.ndarray":
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    return cv2.dilate(mask, kernel)


def _dice(ink_a: "np.ndarray", ink_b: "np.ndarray", valid: "np.ndarray", radius: int) -> float:
    a, b = ink_a & valid, ink_b & valid
    total = int(a.sum()) + int(b.sum())
    if total == 0:
        return 0.0
    matched = int((a & _dilate(b, radius)).sum()) + int((b & _dilate(a, radius)).sum())
    return matched / total


def _affine_from_orb(gray_a: "np.ndarray", gray_b: "np.ndarray", valid: "np.ndarray") -> tuple["np.ndarray | None", dict[str, Any]]:
    cv2.setRNGSeed(SEED)
    orb = cv2.ORB_create(nfeatures=2500, scaleFactor=1.25, nlevels=6, fastThreshold=12)
    mask = (valid * 255).astype(np.uint8)
    ka, da = orb.detectAndCompute(gray_a, mask)
    kb, db = orb.detectAndCompute(gray_b, mask)
    info: dict[str, Any] = {"keypoints": [len(ka or []), len(kb or [])]}
    if da is None or db is None or len(ka) < 12 or len(kb) < 12:
        return None, info
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(db, da, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
    info["matches"] = len(good)
    if len(good) < 12:
        return None, info
    good.sort(key=lambda m: (m.distance, m.queryIdx, m.trainIdx))
    src = np.float32([kb[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([ka[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0, maxIters=2000, confidence=0.99)
    if matrix is None or inliers is None:
        return None, info
    info["inliers"] = int(inliers.sum())
    if int(inliers.sum()) < 10 or inliers.sum() / len(good) < 0.3:
        return None, info
    return matrix.astype(np.float32), info


def _affine_from_ecc(ink_a: "np.ndarray", ink_b: "np.ndarray", valid: "np.ndarray", start: "np.ndarray") -> "np.ndarray | None":
    shrink = 3
    small = lambda m: cv2.resize(cv2.GaussianBlur(m.astype(np.float32) * 255.0, (0, 0), 2.5), None, fx=1 / shrink, fy=1 / shrink, interpolation=cv2.INTER_AREA)  # noqa: E731
    template = small(ink_a * valid)
    moving = small(ink_b * valid)
    warp = start.copy().astype(np.float32)
    warp[:, 2] /= shrink
    try:
        _, warp = cv2.findTransformECC(template, moving, warp, cv2.MOTION_AFFINE, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5), None, 5)
    except cv2.error:
        return None
    warp[:, 2] *= shrink
    return warp


def _warp(image: "np.ndarray", matrix: "np.ndarray", size: tuple[int, int], *, inverse: bool = False) -> "np.ndarray":
    """`matrix` maps points of the moving image to the reference frame (ORB) / reference to moving (ECC, `inverse=True`)."""
    flags = cv2.INTER_NEAREST | (cv2.WARP_INVERSE_MAP if inverse else 0)
    return cv2.warpAffine(image, matrix, size, flags=flags, borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def align(gray_a: "np.ndarray", gray_b: "np.ndarray", ink_a: "np.ndarray", ink_b: "np.ndarray", valid: "np.ndarray", radius: int,
          size: tuple[int, int]) -> tuple["np.ndarray", "np.ndarray", dict[str, Any]]:
    """Returns (matrix B->A as 2x3, aligned ink of B, report). The identity wins ties."""
    identity = np.array([[1, 0, 0], [0, 1, 0]], np.float32)
    candidates: list[tuple[str, "np.ndarray", dict[str, Any]]] = [("identity", identity, {})]
    orb_matrix, orb_info = _affine_from_orb(gray_a, gray_b, valid)
    if orb_matrix is not None:
        candidates.append(("orb", orb_matrix, orb_info))
    ecc_start = orb_matrix if orb_matrix is not None else identity
    # ECC works on the inverse map convention (reference -> moving); invert our B->A matrix for it and back
    inv = cv2.invertAffineTransform(ecc_start)
    ecc = _affine_from_ecc(ink_a, ink_b, valid, inv)
    if ecc is not None:
        candidates.append(("ecc", cv2.invertAffineTransform(ecc), {}))
    best_name, best_matrix, best_info, best_score, best_ink = "identity", identity, {}, -1.0, ink_b
    for name, matrix, info in candidates:
        aligned = ink_b if name == "identity" else _warp(ink_b, matrix, size)
        score = _dice(ink_a, aligned, valid, radius)
        if score > best_score + 1e-6:
            best_name, best_matrix, best_info, best_score, best_ink = name, matrix, info, score, aligned
    scale = float(np.hypot(best_matrix[0, 0], best_matrix[1, 0]))
    rotation = float(np.degrees(np.arctan2(best_matrix[1, 0], best_matrix[0, 0])))
    report = {"method": best_name, "scale": round(scale, 4), "rotation_deg": round(rotation, 3),
              "shift_px": [round(float(best_matrix[0, 2]), 1), round(float(best_matrix[1, 2]), 1)],
              "candidates": {name: round(_dice(ink_a, ink_b if name == "identity" else _warp(ink_b, m, size), valid, radius), 4) for name, m, _ in candidates},
              **{k: v for k, v in best_info.items()}}
    return best_matrix, best_ink, report


# ---------------------------------------------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------------------------------------------
def _canvas(size_a: tuple[float, float], size_b: tuple[float, float]) -> tuple[int, int]:
    long_pt = max(size_a + size_b)
    side = int(min(cfg.MAX_RENDER_SIDE, max(400, round(long_pt / 72.0 * cfg.MAX_DPI))))
    aspect = size_a[1] / size_a[0]
    return (side, max(2, int(round(side * aspect)))) if aspect <= 1 else (max(2, int(round(side / aspect))), side)


def aspect_compatible(size_a: tuple[float, float], size_b: tuple[float, float]) -> bool:
    ra, rb = size_a[0] / size_a[1], size_b[0] / size_b[1]
    return abs(ra - rb) / max(ra, rb) <= cfg.ASPECT_TOLERANCE


def compare_images(pdf_a: bytes, page_a: int, pdf_b: bytes, page_b: int, *, want_overlay: bool = False) -> dict[str, Any]:
    """Raster comparison of two pages. Raises RuntimeError without OpenCV, ValueError for a bad page."""
    if not available():
        raise RuntimeError("OpenCV is not installed")
    size_a_pt, size_b_pt = _page_size(pdf_a, page_a), _page_size(pdf_b, page_b)
    base: dict[str, Any] = {"model_version": cfg.MODEL_VERSION, "left_size_pt": [round(v, 2) for v in size_a_pt], "right_size_pt": [round(v, 2) for v in size_b_pt]}
    if not aspect_compatible(size_a_pt, size_b_pt):
        return {**base, "comparable": False, "reason": "format_mismatch", "similarity": None, "regions": []}
    size = _canvas(size_a_pt, size_b_pt)
    gray_a = _render_gray(pdf_a, page_a, size)
    gray_b = _render_gray(pdf_b, page_b, size)
    valid = _valid_mask(size, *size_a_pt)
    ink_a, ink_b = _ink(gray_a), _ink(gray_b)
    area = float(valid.sum()) or 1.0
    radius = _tolerance_px(size, size_a_pt[0])
    ink_share = [round(float((ink_a & valid).sum()) / area, 5), round(float((ink_b & valid).sum()) / area, 5)]
    base.update({"canvas_px": list(size), "tolerance_px": radius, "ink_share": ink_share})
    if min(ink_share) < 0.0004:
        return {**base, "comparable": False, "reason": "no_strokes", "similarity": None, "regions": []}
    matrix, ink_b_aligned, alignment = align(gray_a, gray_b, ink_a, ink_b, valid, radius, size)
    a, b = ink_a & valid, ink_b_aligned & valid
    near_b, near_a = _dilate(b, radius), _dilate(a, radius)
    only_a, only_b = a & (1 - near_b), b & (1 - near_a)
    matched = int((a & near_b).sum()) + int((b & near_a).sum())
    total = int(a.sum()) + int(b.sum())
    similarity = matched / total if total else None
    regions, diffuse = _regions(only_a, only_b, matrix, size, size_a_pt, size_b_pt, float(valid.sum()))
    layout = None if similarity is None else ("same" if similarity >= LAYOUT_SAME else ("partial" if similarity >= LAYOUT_PARTIAL else "different"))
    result = {**base, "comparable": True, "reason": None, "similarity": round(similarity, 4) if similarity is not None else None,
              "layout_match": layout, "diffuse_share": diffuse,
              "alignment": alignment, "regions": regions if layout != "different" else [],
              "totals": {"left_strokes_px": int(a.sum()), "right_strokes_px": int(b.sum()), "only_left_px": int(only_a.sum()), "only_right_px": int(only_b.sum())}}
    if want_overlay:
        result["_overlay"] = _overlay(a, b, only_a, only_b)
    return result


def _regions(only_a: "np.ndarray", only_b: "np.ndarray", matrix: "np.ndarray", size: tuple[int, int], size_a_pt: tuple[float, float],
             size_b_pt: tuple[float, float], valid_area: float) -> tuple[list[dict[str, Any]], float]:
    """(regions, diffuse_share): a cluster that covers more than a third of the drawing area is not a localised difference
    (two different compositions of the sheet); its strokes are counted in `diffuse_share`, not listed as a region."""
    w, h = size
    diff = (only_a | only_b).astype(np.uint8)
    if not diff.any():
        return [], 0.0
    diffuse_px = 0
    mm_per_px = (size_a_pt[0] * 25.4 / 72.0) / w
    merge = max(3, int(round(3.5 / mm_per_px)))              # strokes within ~3.5 mm form one region
    clustered = cv2.morphologyEx(diff, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (merge, merge)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(clustered, connectivity=8)
    inverse = cv2.invertAffineTransform(matrix)
    out = []
    for index in range(1, count):
        x, y, bw, bh, _area = stats[index]
        box_area_mm2 = bw * bh * mm_per_px * mm_per_px
        patch = labels[y:y + bh, x:x + bw] == index
        left_px = int((only_a[y:y + bh, x:x + bw] & patch).sum())
        right_px = int((only_b[y:y + bh, x:x + bw] & patch).sum())
        if bw * bh > DIFFUSE_AREA_SHARE * valid_area:
            diffuse_px += left_px + right_px
            continue
        if left_px + right_px < 8 or box_area_mm2 < cfg.MIN_REGION_MM2:
            continue
        corners = np.array([[x, y], [x + bw, y], [x, y + bh], [x + bw, y + bh]], np.float32)
        mapped = (corners @ inverse[:, :2].T) + inverse[:, 2]
        bx0, by0, bx1, by1 = (float(mapped[:, 0].min()), float(mapped[:, 1].min()), float(mapped[:, 0].max()), float(mapped[:, 1].max()))
        out.append({
            "bbox_left": [round(x / w * size_a_pt[0], 1), round(y / h * size_a_pt[1], 1), round((x + bw) / w * size_a_pt[0], 1), round((y + bh) / h * size_a_pt[1], 1)],
            "bbox_right": [round(max(0.0, bx0) / w * size_b_pt[0], 1), round(max(0.0, by0) / h * size_b_pt[1], 1),
                           round(min(float(w), bx1) / w * size_b_pt[0], 1), round(min(float(h), by1) / h * size_b_pt[1], 1)],
            "area_mm2": round(box_area_mm2, 1), "only_left_px": left_px, "only_right_px": right_px,
            "kind": "only_left" if right_px < 0.2 * left_px else ("only_right" if left_px < 0.2 * right_px else "changed"),
        })
    out.sort(key=lambda r: (-(r["only_left_px"] + r["only_right_px"]), r["bbox_left"]))
    total = sum(r["only_left_px"] + r["only_right_px"] for r in out) or 1
    for number, region in enumerate(out[:cfg.MAX_REGIONS], 1):
        region["id"] = number
        region["share"] = round((region["only_left_px"] + region["only_right_px"]) / total, 4)
    return out[:cfg.MAX_REGIONS], round(diffuse_px / max(1, int(diff.sum())), 4)


def _overlay(a: "np.ndarray", b: "np.ndarray", only_a: "np.ndarray", only_b: "np.ndarray") -> bytes:
    """White sheet; strokes on both sheets light grey, only on the left red, only on the right blue-green (BGR image)."""
    h, w = a.shape
    image = np.full((h, w, 3), 255, np.uint8)
    image[(a | b) > 0] = (200, 200, 200)
    thick = cv2.dilate(only_a, np.ones((2, 2), np.uint8))
    image[thick > 0] = (60, 60, 230)
    thick = cv2.dilate(only_b, np.ones((2, 2), np.uint8))
    image[thick > 0] = (170, 140, 0)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return encoded.tobytes()


# ---------------------------------------------------------------------------------------------------------------
# cached entry point
# ---------------------------------------------------------------------------------------------------------------
def pair_key(sha_a: str, page_a: int, sha_b: str, page_b: int) -> str:
    return hashlib.sha256(f"{sha_a}:{page_a}|{sha_b}:{page_b}|{fingerprint()}".encode()).hexdigest()[:40]


@lru_cache(maxsize=16)
def _memo(key: str, loader: Any) -> tuple[dict[str, Any], bytes | None]:
    return loader()


def compare_cached(pdf_a: bytes, sha_a: str, page_a: int, pdf_b: bytes, sha_b: str, page_b: int, *, want_overlay: bool = False) -> tuple[dict[str, Any], bytes | None]:
    """(result, overlay_png | None); the result is cached per SHA-256 of the two files and pages (JSON + PNG on disk when a cache dir is set)."""
    key = pair_key(sha_a, page_a, sha_b, page_b)
    directory = cache_dir()
    json_path = directory / "pairs" / f"{key}.json" if directory else None
    png_path = directory / "pairs" / f"{key}.png" if directory else None
    if json_path is not None and json_path.is_file() and (not want_overlay or (png_path is not None and png_path.is_file())):
        try:
            result = json.loads(json_path.read_text(encoding="utf-8"))
            overlay = png_path.read_bytes() if want_overlay and result.get("comparable") else None
            return result, overlay
        except (OSError, ValueError):
            pass
    result = compare_images(pdf_a, page_a, pdf_b, page_b, want_overlay=True)
    overlay = result.pop("_overlay", None)
    if json_path is not None:
        try:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = json_path.with_name(f"{json_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, json_path)
            if overlay is not None and png_path is not None:
                tmp_png = png_path.with_name(f"{png_path.name}.{os.getpid()}.tmp")
                tmp_png.write_bytes(overlay)
                os.replace(tmp_png, png_path)
        except OSError as exc:
            logger.warning("sheet compare cache write failed: %s", exc)
    return result, (overlay if want_overlay else None)
