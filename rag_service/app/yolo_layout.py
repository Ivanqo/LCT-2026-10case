from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image  # type: ignore

logger = logging.getLogger(__name__)

# Default lightweight DocLayNet model (YOLOv8n) weights from ppaanngggg/yolo-doclaynet
DEFAULT_MODEL_URL = "https://github.com/ppaanngggg/yolo-doclaynet/raw/main/yolo-doclaynet.pt"

# DocLayNet labels used by the model (common mapping)
# We only care about TABLE and PICTURE (as drawing/figure).
DOC_LABEL_MAP = {
    "Table": "table",
    "Picture": "drawing",
    "Caption": "caption",
    "Title": "title",
    "Text": "text",
    "Section-header": "header",
    "List-item": "list_item",
    "Formula": "formula",
    "Footnote": "footnote",
    "Page-header": "page_header",
    "Page-footer": "page_footer",
}


def _map_label(label: str) -> Optional[str]:
    lab = (label or "").strip().lower().replace("_", "-")
    # normalize some common variants
    if lab in ("table", "tabular", "tab"):
        return "table"
    if lab in ("picture", "figure", "image", "diagram", "drawing", "photo", "illustration"):
        return "drawing"
    # DocLayNet names (varies by weights)
    if "table" in lab:
        return "table"
    if any(k in lab for k in ("picture", "figure", "image", "diagram", "drawing", "illustrat")):
        return "drawing"
    return None
    # direct
    if label in DOC_LABEL_MAP:
        return DOC_LABEL_MAP[label]
    l = label.strip().lower().replace("_", "-")
    # common variants
    if l in ("table",):
        return "table"
    if l in ("picture", "figure", "image", "graphic", "diagram", "drawing", "chart", "photo"):
        return "drawing"
    # some models use numbers as strings
    return DOC_LABEL_MAP.get(label)


@dataclass
class YoloDet:
    region_type: str
    bbox: List[int]  # [x0,y0,x1,y1] px
    conf: float
    label: str


def _ensure_weights(path: Path, *, url: str = DEFAULT_MODEL_URL) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 1024 * 1024:
        return path

    # download once
    import requests

    logger.info("Downloading YOLO layout weights to %s ...", path)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    return path


def _load_model(weights_path: Path):
    try:
        from ultralytics import YOLO  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Ultralytics YOLO is not installed. Install: pip install ultralytics torch torchvision"
        ) from e
    return YOLO(str(weights_path))


def detect_layout_yolo(
    img: Image.Image,
    *,
    weights_path: Path,
    conf: float = 0.25,
    iou: float = 0.45,
    max_det: int = 50,
    device: Optional[str] = None,
) -> List[YoloDet]:
    """
    Run lightweight YOLO layout detection on a page image.

    Returns a list of detections with mapped region_type for our pipeline.
    """
    weights_path = _ensure_weights(weights_path)
    model = _load_model(weights_path)

    # Ultralytics accepts PIL directly
    kwargs = {"conf": conf, "iou": iou, "max_det": max_det, "verbose": False}
    if device:
        kwargs["device"] = device

    res = model.predict(source=img, **kwargs)
    if not res:
        return []

    r0 = res[0]
    names: Dict[int, str] = getattr(r0, "names", {}) or {}
    dets: List[YoloDet] = []

    boxes = getattr(r0, "boxes", None)
    if boxes is None:
        return []

    # boxes.xyxy, boxes.conf, boxes.cls
    xyxy = boxes.xyxy.cpu().numpy()  # type: ignore
    confs = boxes.conf.cpu().numpy()  # type: ignore
    clss = boxes.cls.cpu().numpy().astype(int)  # type: ignore

    for (x0, y0, x1, y1), c, ci in zip(xyxy, confs, clss):
        label = names.get(int(ci), str(ci))
        mapped = _map_label(label)
        if not mapped:
            continue
        # We only emit what our pipeline uses directly
        if mapped not in ("table", "drawing"):
            continue
        bbox = [int(max(0, x0)), int(max(0, y0)), int(min(img.width, x1)), int(min(img.height, y1))]
        if bbox[2] - bbox[0] < 20 or bbox[3] - bbox[1] < 20:
            continue
        dets.append(YoloDet(region_type=mapped, bbox=bbox, conf=float(c), label=label))

    # sort by confidence
    dets.sort(key=lambda d: d.conf, reverse=True)
    return dets
