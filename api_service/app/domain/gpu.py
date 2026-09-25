"""Runtime GPU detection and startup observability for CUDA-backed optional
dependencies (currently: the semantic-anchor `SentenceTransformer` in
`semantic_similarity.py`). Mirrors the "never degrade silently" pattern this
project already applies to the tesseract OCR fallback (see
`dataset_sources.check_tesseract_health` / `app/main.py`'s startup hook):
when an operator sets `CASE10_EXPECT_GPU=1` (the organizer's grading server
is documented to hand each team a shared physical NVIDIA H100 via the NVIDIA
Container Toolkit) but torch reports no CUDA device, that is a misconfigured
deployment and must log loudly -- not fall back to CPU inference unnoticed.

Never imports torch at module import time, so torch/sentence-transformers
stay optional dependencies for the rest of the app exactly as
`semantic_similarity.py` already established -- the import is deferred into
`detect_gpu()`, which every caller in this process shares through the module-
level cache (`detect_gpu()` actually probes and is meant to run once, at API
startup; everyone else reads `gpu_health_status()`/`preferred_device()`).

Deliberately does not (and must not) install the NVIDIA driver or reference
one: the driver is provided by the host via `--gpus`/the NVIDIA Container
Toolkit, never bundled into the image (see api_service/Dockerfile's torch
install step). This module only ever asks torch what it can already see.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ..metrics import gpu_available

logger = logging.getLogger(__name__)

# The grading server's GPU is shared across teams during evaluation
# ("несколько команд могут одновременно использовать один физический GPU...
# нельзя рассчитывать на монопольный доступ ко всем 80 ГБ") -- this process
# must never try to grab the whole device for itself. Kept low: the only GPU
# consumer today is a ~460MB sentence-embedding model plus small batch
# activations (see semantic_similarity.py), nowhere near needing a large
# fraction of an H100's 80GB.
GPU_MEMORY_FRACTION = float(os.getenv("CASE10_GPU_MEMORY_FRACTION", "0.2"))
# Set by ops on hosts that are supposed to have a GPU (the grading server) so
# a driver/toolkit misconfiguration there is a loud WARNING, not a silent
# CPU fallback that only shows up later as unexplained latency.
EXPECT_GPU = os.getenv("CASE10_EXPECT_GPU", "0").strip().lower() not in ("0", "false", "no", "")

_cached_status: dict[str, Any] | None = None


def detect_gpu() -> dict[str, Any]:
    """Detects CUDA availability via `torch.cuda.is_available()`, applies the
    per-process memory fraction cap when a GPU is present, logs the outcome
    (WARNING instead of the usual INFO when `CASE10_EXPECT_GPU=1` and no GPU
    was found) and caches the result for `gpu_health_status()` (e.g. the
    `/health` endpoint) to read without re-probing. Never raises -- nothing
    else in this process depends on a GPU being present, exactly like
    `check_tesseract_health()` for the OCR fallback."""
    global _cached_status
    status: dict[str, Any] = {
        "available": False,
        "device_name": None,
        "device_count": 0,
        "memory_fraction_limit": GPU_MEMORY_FRACTION,
        "error": None,
    }
    try:
        import torch
    except Exception as exc:
        status["error"] = f"torch not importable: {exc!r}"
        gpu_available.set(0)
        _log_result(status)
        _cached_status = status
        return status

    try:
        status["available"] = bool(torch.cuda.is_available())
    except Exception as exc:
        status["error"] = f"torch.cuda.is_available() raised: {exc!r}"
        gpu_available.set(0)
        _log_result(status)
        _cached_status = status
        return status

    if status["available"]:
        try:
            status["device_count"] = torch.cuda.device_count()
            status["device_name"] = torch.cuda.get_device_name(0)
            torch.cuda.set_per_process_memory_fraction(GPU_MEMORY_FRACTION, device=0)
        except Exception as exc:
            status["error"] = f"GPU detected but could not be configured: {exc!r}"
            logger.warning("GPU detected but could not be configured: %s", exc)

    gpu_available.set(1 if status["available"] else 0)
    _log_result(status)
    _cached_status = status
    return status


def _log_result(status: dict[str, Any]) -> None:
    if status["available"]:
        logger.info(
            "GPU detected: %s (device_count=%s), per-process memory fraction capped at %.0f%%",
            status["device_name"], status["device_count"], GPU_MEMORY_FRACTION * 100,
        )
    elif EXPECT_GPU:
        logger.warning(
            "CASE10_EXPECT_GPU=1 but no CUDA device found (%s) -- falling back to CPU inference; "
            "check the NVIDIA Container Toolkit / `--gpus` wiring on this host",
            status.get("error") or "torch.cuda.is_available() returned False",
        )
    else:
        logger.info("No GPU detected -- running on CPU (%s)", status.get("error") or "none expected on this host")


def gpu_health_status() -> dict[str, Any]:
    """Last-known result of `detect_gpu()` (run once at API startup), for
    cheap reuse by e.g. the `/health` endpoint -- reading this never
    re-imports torch or re-probes CUDA. Runs detection on first use if
    startup somehow never called it (e.g. a test importing this module
    directly), so this is always safe to call."""
    if _cached_status is None:
        return detect_gpu()
    return dict(_cached_status)


def preferred_device() -> str:
    """`"cuda"` when a GPU was detected (at startup, or on first use), else
    `"cpu"`. Callers (e.g. `semantic_similarity._get_model()`) pass this
    straight to `SentenceTransformer(device=...)` instead of relying on its
    own implicit auto-detection, so the memory-fraction cap set by
    `detect_gpu()` is always applied before any model actually gets loaded
    onto the device."""
    return "cuda" if gpu_health_status()["available"] else "cpu"
