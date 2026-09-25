from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from PIL import Image  # type: ignore

logger = logging.getLogger(__name__)

try:
    import pytesseract  # type: ignore
except Exception:  # pragma: no cover
    pytesseract = None


@dataclass
class OCRWord:
    text: str
    bbox: List[int]  # [x0,y0,x1,y1] in image px
    conf: float = 0.0


@dataclass
class OCRResult:
    text: str
    words: List[OCRWord]

def get_available_langs() -> List[str]:
    """Return languages available in the local Tesseract installation."""
    _require_pytesseract()
    try:
        return list(pytesseract.get_languages(config=""))  # type: ignore
    except Exception as e:
        logger.warning("Could not list tesseract languages: %s", e)
        return []


def normalize_lang_request(lang: str) -> str:
    """
    Remove unavailable languages from a request like "rus+eng".
    If nothing is available, falls back to "eng".
    """
    avail = set(get_available_langs())
    if not avail:
        return "eng"
    parts = [p.strip() for p in (lang or "").split("+") if p.strip()]
    kept = [p for p in parts if p in avail]
    if not kept:
        # common case: rus missing -> fallback to eng so OCR doesn't hang
        return "eng" if "eng" in avail else parts[0]
    return "+".join(kept)



def _require_pytesseract() -> None:
    if pytesseract is None:
        raise RuntimeError("pytesseract is not installed. pip install pytesseract")


def _downscale(img: Image.Image, max_side: int) -> Image.Image:
    w, h = img.size
    s = max(w, h)
    if max_side and s > max_side:
        scale = max_side / float(s)
        return img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    return img



def _preprocess_table(img: Image.Image) -> Image.Image:
    """
    Preprocess table crop for better OCR on CAD sheets:
      - convert to grayscale
      - increase contrast slightly
      - binarize (adaptive if available)
      - upscale 2x for small fonts (bounded later by max_side)
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        arr = np.array(img.convert("L"))
        # CLAHE contrast
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        arr = clahe.apply(arr)
        thr = cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10)
        out = Image.fromarray(thr).convert("RGB")
    except Exception:
        out = img.convert("L").convert("RGB")

    # Upscale only for small crops (keeps large tables fast)
    w, h = out.size
    if max(w, h) < 900:
        return out.resize((w * 2, h * 2))
    return out

def ocr_image_fast(
    img: Image.Image,
    *,
    lang: str = "rus+eng",
    timeout_sec: int = 12,
    max_side: int = 1600,
    config: str = "",
) -> OCRResult:
    """Fast OCR (image_to_string) with downscale + timeout. Never returns word boxes."""
    _require_pytesseract()
    img2 = _downscale(img, max_side=max_side).convert("RGB")
    try:
        text = pytesseract.image_to_string(img2, lang=lang, config=config or "", timeout=timeout_sec)  # type: ignore
    except Exception as e:
        logger.warning("Fast OCR failed: %s", e)
        text = ""
    return OCRResult(text=(text or "").strip(), words=[])


def ocr_image(
    img: Image.Image,
    *,
    lang: str = "rus+eng",
    timeout_sec: int = 12,
    max_side: int = 1800,
    config: str = "",
) -> OCRResult:
    """Word-level OCR (image_to_data). Slow; includes downscale + timeout. On failure returns empty."""
    _require_pytesseract()
    img2 = _downscale(img, max_side=max_side).convert("RGB")
    try:
        data = pytesseract.image_to_data(
            img2,
            lang=lang,
            config=config or "",
            timeout=timeout_sec,
            output_type=pytesseract.Output.DICT,  # type: ignore
        )  # type: ignore
        n = len(data.get("text", []))
        words: List[OCRWord] = []
        parts: List[str] = []
        for i in range(n):
            t = (data["text"][i] or "").strip()
            if not t:
                continue
            try:
                conf = float(data.get("conf", [0])[i])
            except Exception:
                conf = 0.0
            x, y, w, h = int(data["left"][i]), int(data["top"][i]), int(data["width"][i]), int(data["height"][i])
            words.append(OCRWord(text=t, bbox=[x, y, x + w, y + h], conf=conf))
            parts.append(t)
        text = " ".join(parts).strip()
        return OCRResult(text=text, words=words)
    except Exception as e:
        logger.warning("Word-level OCR failed: %s", e)
        return OCRResult(text="", words=[])


def ocr_image_tiled(
    img: Image.Image,
    *,
    lang: str = "rus+eng",
    timeout_sec: int = 8,
    max_side: int = 1400,
    tiles_x: int = 2,
    tiles_y: int = 2,
    config: str = "",
) -> OCRResult:
    """Robust OCR for large tables/dense regions.

    Guarantees progress by splitting into tiles and applying a short timeout per tile.
    Returns text only (words=[]).
    """
    _require_pytesseract()
    img2 = _downscale(img, max_side=max_side).convert("RGB")
    w, h = img2.size
    tiles_x = max(1, int(tiles_x))
    tiles_y = max(1, int(tiles_y))
    tw = max(1, w // tiles_x)
    th = max(1, h // tiles_y)

    parts: List[str] = []
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            x0 = tx * tw
            y0 = ty * th
            x1 = w if tx == tiles_x - 1 else (tx + 1) * tw
            y1 = h if ty == tiles_y - 1 else (ty + 1) * th
            tile = img2.crop((x0, y0, x1, y1))
            try:
                t = pytesseract.image_to_string(tile, lang=lang, config=config or "", timeout=timeout_sec)  # type: ignore
                t = (t or "").strip()
                if t:
                    parts.append(t)
            except Exception as e:
                logger.warning("Tiled OCR tile failed (tx=%s ty=%s): %s", tx, ty, e)
                continue

    return OCRResult(text="\n".join(parts).strip(), words=[])


def ocr_image_table_fast(
    img: Image.Image,
    *,
    lang: str = "rus+eng",
    timeout_sec: int = 12,
    max_side: int = 1800,
) -> OCRResult:
    """
    Table-oriented fast OCR with preprocessing + good PSM.
    """
    cfg = "--oem 1 --psm 6"
    # Downscale first, then preprocess (much faster on large tables)
    img2 = _downscale(img, max_side=max_side).convert("RGB")
    img2 = _preprocess_table(img2)
    return ocr_image_fast(img2, lang=lang, timeout_sec=timeout_sec, max_side=max_side, config=cfg)


def ocr_image_table_tiled(
    img: Image.Image,
    *,
    lang: str = "rus+eng",
    timeout_sec: int = 8,
    max_side: int = 1600,
    tiles_x: int = 2,
    tiles_y: int = 2,
) -> OCRResult:
    """
    Table-oriented tiled OCR with preprocessing + good PSM.
    """
    cfg = "--oem 1 --psm 6"
    # Downscale first, then preprocess (much faster on large tables)
    img2 = _downscale(img, max_side=max_side).convert("RGB")
    img2 = _preprocess_table(img2)
    return ocr_image_tiled(img2, lang=lang, timeout_sec=timeout_sec, max_side=max_side, tiles_x=tiles_x, tiles_y=tiles_y, config=cfg)
