"""Optional PaddleOCR full-page OCR-fallback engine (Промпт 8.2,
CASE10_AGENT_PROMPTS_BACKLOG.md: "PaddleOCR (GPU-режим) как альтернативный OCR-путь...
переключаемый по конфигурации"). Sits alongside the existing tesseract path in
`dataset_sources.py` -- selected by `settings.OCR_ENGINE`, never the only path: a load or
inference failure here always falls back to tesseract rather than returning no OCR at all
(mirrors `semantic_similarity.py`'s "degrade to no-op, never crash the caller" pattern, and
`gpu.py`'s "never degrade silently" logging).

**Model choice, empirically verified rather than assumed** (see
evaluation/OCR_PADDLEOCR_PILOT_REPORT.md for the full accuracy comparison against
tesseract on this corpus): `lang="ru"` resolves to PaddleOCR's `eslav_PP-OCRv5_mobile_rec`
recognition model (the `ru`/`be`/`uk` East Slavic group -- a narrower, better-targeted
group than the full `CYRILLIC_LANGS` set PaddleOCR also ships, which additionally covers
~30 Caucasian/Turkic languages this corpus never contains) paired with the default
`PP-OCRv5_server_det` text-detection model. Unlike tesseract's single combined "rus+eng"
pass, PaddleOCR is one-language-per-pipeline; this corpus is overwhelmingly Russian
construction documentation (see module docstring precedent in `semantic_similarity.py`),
so a single "ru" pass is the right default rather than adding a second "en" pass and a
merge step with no evidence it is needed.

Never imports `paddleocr`/`paddlex`/`paddle` at module import time -- deferred into
`_get_pipeline()`, exactly like `semantic_similarity._get_model()` defers `torch`/
`sentence_transformers` -- so these stay optional dependencies for every caller that never
sets `CASE10_OCR_ENGINE=paddleocr`.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ..metrics import ocr_paddleocr_available, ocr_paddleocr_calls_total

logger = logging.getLogger(__name__)

# Same rationale as `gpu.GPU_MEMORY_FRACTION`: the grading server's H100 is shared
# across teams, and this must never try to grab the whole device for itself. A
# SEPARATE env var/flag from torch's cap -- paddle's CUDA allocator is an independent
# process from torch's (both loaded in the same Python process here, one for
# semantic_similarity.py's SentenceTransformer, one for this module), so torch's
# `set_per_process_memory_fraction` call has no effect on paddle's allocations.
# `FLAGS_fraction_of_gpu_memory_to_use` is paddle's own classic mechanism for this
# (there is no `paddle.device.cuda.set_per_process_memory_fraction` equivalent to
# torch's in this paddle version, confirmed by inspecting `dir(paddle.device.cuda)`)
# -- it must be set before paddle's CUDA context initializes, so this is applied right
# before the first `import paddleocr` below, not at module import time.
OCR_PADDLE_GPU_MEMORY_FRACTION = float(os.getenv("CASE10_OCR_PADDLE_GPU_MEMORY_FRACTION", "0.2"))

_pipelines: dict[tuple[str, str], Any] = {}
_pipeline_load_failed: dict[tuple[str, str], bool] = {}
_health_cache: dict[str, Any] = {"available": None, "error": "never checked"}


def _paddle_supports_gpu() -> bool:
    """`gpu.preferred_device()` answers "does torch see a CUDA device" -- a DIFFERENT
    question from "was the currently-installed `paddle` package itself compiled with
    CUDA support". The two can disagree for a real, confirmed reason (see
    evaluation/OCR_PADDLEOCR_PILOT_REPORT.md): shipping `paddlepaddle-gpu` alongside
    torch in the same image breaks torch's own CUDA libs (a pinned nvidia-cudnn-cu12
    version conflict), so this image ships plain CPU `paddlepaddle` -- on the grading
    server's real H100, torch would correctly report `cuda` available while paddle
    itself has no CUDA support compiled in at all. Blindly trusting torch's answer here
    would make every PaddleOCR call request `device="gpu:0"` from a CPU-only paddle
    build, fail, and permanently cache itself as unavailable -- silently disabling the
    whole engine on exactly the host it is meant to run on. Checking paddle's own
    compiled-in capability instead of torch's independently-answered question is what
    prevents that."""
    try:
        import paddle

        return bool(paddle.device.is_compiled_with_cuda())
    except Exception:
        return False


def _paddle_device(torch_device: str) -> str:
    """`gpu.preferred_device()` returns torch's `"cuda"`/`"cpu"` vocabulary --
    PaddleOCR/PaddleX wants `"gpu:0"`/`"cpu"`. Kept as its own function (not inlined)
    because the two frameworks' device strings are a real, easy-to-get-wrong translation,
    not a coincidence worth relying on. Also downgrades to `"cpu"` whenever the
    installed `paddle` package itself has no CUDA support compiled in, regardless of
    what torch/`gpu.py` answers -- see `_paddle_supports_gpu()`."""
    if torch_device == "cuda" and _paddle_supports_gpu():
        return "gpu:0"
    return "cpu"


def _get_pipeline(lang: str):
    """Lazy singleton per (lang, device) -- constructing a `PaddleOCR` pipeline loads
    real model weights from disk and is far too slow to repeat on every page. Any
    failure (missing dependency, no cached model and no network, unsupported lang, a
    broken inference backend) is cached as a permanent "unavailable" for that (lang,
    device) pair for the rest of the process, exactly like
    `semantic_similarity._get_model()` -- we do not retry a slow failure on every
    subsequent page."""
    from .gpu import preferred_device

    device = _paddle_device(preferred_device())
    key = (lang, device)
    if _pipeline_load_failed.get(key):
        return None
    if key in _pipelines:
        return _pipelines[key]
    if device.startswith("gpu") and "FLAGS_fraction_of_gpu_memory_to_use" not in os.environ:
        os.environ["FLAGS_fraction_of_gpu_memory_to_use"] = str(OCR_PADDLE_GPU_MEMORY_FRACTION)
    try:
        from paddleocr import PaddleOCR

        pipeline = PaddleOCR(
            lang=lang,
            device=device,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            # Confirmed real bug (not a guess): PP-OCRv5_server_det's default oneDNN
            # kernel under PaddlePaddle 3.3.1's PIR executor raises `NotImplementedError:
            # ConvertPirAttribute2RuntimeAttribute not support
            # [pir::ArrayAttribute<pir::DoubleAttribute>]` on `predictor.run()` -- 100%
            # reproducible on this project's own CPU inference path (see
            # evaluation/OCR_PADDLEOCR_PILOT_REPORT.md). oneDNN is a CPU-only backend
            # (irrelevant once `device="gpu:0"` picks the CUDA execution path on the
            # grading server's H100), so disabling it unconditionally here only costs
            # CPU-path inference speed, never accuracy or the GPU path.
            enable_mkldnn=False,
        )
    except Exception:
        logger.exception("PaddleOCR pipeline failed to load for lang=%s device=%s", lang, device)
        _pipeline_load_failed[key] = True
        return None
    _pipelines[key] = pipeline
    return pipeline


def _extract_result_fields(result: Any) -> tuple[list[str], list[float], list[list[float]]]:
    """PaddleX 3.x pipeline results are dict-like (`result["rec_texts"]`) as well as
    attribute-accessible in some versions -- try dict access first (the documented,
    stable public shape) and fall back to attributes so a minor PaddleX version bump
    does not silently turn into an empty-words result."""
    def _get(field: str):
        try:
            return result[field]
        except (TypeError, KeyError, IndexError):
            return getattr(result, field, None)

    texts = list(_get("rec_texts") or [])
    scores = list(_get("rec_scores") or [])
    boxes = _get("rec_boxes")
    boxes = boxes.tolist() if hasattr(boxes, "tolist") else list(boxes or [])
    return texts, scores, boxes


def recognize_words(image, lang: str) -> list[dict[str, Any]] | None:
    """Runs the configured PaddleOCR pipeline against a PIL image (same full-page
    render `dataset_sources._ocr_page_words` already builds for tesseract) and returns
    word boxes in the SAME shape tesseract's TSV parser produces -- `{"text", "bbox"
    (pixel space, [left, top, right, bottom]), "confidence"}` -- so the caller's
    zoom-descale-to-PDF-points step is engine-agnostic. Returns `None` (never an empty
    list disguised as success) when the pipeline is unavailable, so the caller can tell
    "no engine" apart from "engine ran, found nothing" and fall back to tesseract only
    in the former case."""
    pipeline = _get_pipeline(lang)
    if pipeline is None:
        ocr_paddleocr_calls_total.labels(outcome="unavailable").inc()
        return None
    try:
        import numpy as np

        array = np.array(image.convert("RGB"))
        results = list(pipeline.predict(array))
    except Exception:
        logger.exception("PaddleOCR predict() failed for lang=%s", lang)
        ocr_paddleocr_calls_total.labels(outcome="exception").inc()
        return None
    if not results:
        ocr_paddleocr_calls_total.labels(outcome="empty_success").inc()
        return []
    texts, scores, boxes = _extract_result_fields(results[0])
    words = []
    for index, text in enumerate(texts):
        text = str(text).strip()
        if not text or index >= len(boxes):
            continue
        box = boxes[index]
        if len(box) != 4:
            continue
        words.append({
            "text": text,
            "bbox": [float(box[0]), float(box[1]), float(box[2]), float(box[3])],
            "confidence": float(scores[index]) if index < len(scores) else None,
        })
    ocr_paddleocr_calls_total.labels(outcome="success" if words else "empty_success").inc()
    return words


_HEALTH_CHECK_IMAGE = None


def _health_check_image():
    global _HEALTH_CHECK_IMAGE
    if _HEALTH_CHECK_IMAGE is None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (240, 60), (255, 255, 255))
        ImageDraw.Draw(image).text((10, 15), "Проверка OCR", fill=(0, 0, 0))
        _HEALTH_CHECK_IMAGE = image
    return _HEALTH_CHECK_IMAGE


def check_paddleocr_health() -> dict[str, Any]:
    """Startup/ops health check, mirroring `dataset_sources.check_tesseract_health()`:
    actually runs the configured pipeline against a tiny synthetic image (not just
    "did the import succeed") and caches the result for `paddleocr_health_status()`
    (e.g. the `/health` endpoint). Never raises -- `settings.OCR_ENGINE=paddleocr`
    falling back to tesseract on a broken pipeline must be loud, but not fatal to
    startup, exactly like a broken tesseract is not fatal today."""
    from ..config import settings

    result: dict[str, Any] = {"available": False, "error": None}
    words = recognize_words(_health_check_image(), settings.OCR_PADDLE_LANG)
    if words is None:
        result["error"] = "PaddleOCR pipeline failed to load or run (see logs for the exception)"
        ocr_paddleocr_available.set(0)
        logger.critical("PaddleOCR health check failed: %s", result["error"])
    else:
        result["available"] = True
        ocr_paddleocr_available.set(1)
        logger.info("PaddleOCR health check ok (lang=%s)", settings.OCR_PADDLE_LANG)
    _health_cache.update(result)
    return result


def paddleocr_health_status() -> dict[str, Any]:
    return dict(_health_cache)
