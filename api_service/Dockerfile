# Digest-pinned (not just tag-pinned): the apt archive snapshot a Debian base image
# resolves to moves with the image, so pinning only the tesseract-ocr package version
# below is not by itself reproducible -- a later `python:3.11-slim` pull can land on a
# newer Debian snapshot where that exact package build no longer exists, breaking the
# build outright. Pinning the digest freezes the apt snapshot these versions were
# verified against (2026-09-20: Debian 13 "trixie", resolves the tesseract-ocr --psm
# double-dash-flag incompatibility found when auditing OCR fallback observability --
# see evaluation/OCR_GOLD_REVIEW_PASS1_REPORT.md finding #1).
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Versions pinned to what the digest above actually serves (confirmed 2026-09-20 with a
# --no-cache build): tesseract-ocr 5.5.0-1+b1 supports the `--psm` (double-dash) syntax
# `dataset_sources.py` calls it with; bumping the base image digest requires re-verifying
# (or re-pinning) these versions together with it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr=5.5.0-1+b1 \
        tesseract-ocr-rus=1:4.1.0-2 \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
# libgl1/libglib2.0-0 above: opencv-python (a transitive paddleocr/paddlex dependency,
# Промпт 8.2) dynamically loads libGL.so.1 at import time even though this is a headless
# server image with no display -- confirmed real (`ImportError: libGL.so.1: cannot open
# shared object file`, not a hypothetical) on the very first build attempt. Left
# unpinned unlike tesseract above: these are transitive runtime shared libraries opencv
# dlopens, never invoked by version-sensitive flag/name the way the tesseract binary is,
# so a Debian point-release bump here carries materially lower reproducibility risk.
COPY api_service/requirements.txt ./requirements.txt

# PyTorch built against CUDA 12.1 (Hopper/sm_90 kernels included since
# PyTorch 2.1 -- covers the NVIDIA H100 the organizer's grading server passes
# into each team's container via the NVIDIA Container Toolkit; CUDA >=12.0 /
# compute capability 9.0 per the organizer's confirmed spec). This wheel pulls
# in the CUDA *runtime* shared libraries as ordinary pip deps (nvidia-cublas-
# cu12, nvidia-cudnn-cu12, ...) -- never the NVIDIA driver itself, which
# participants are explicitly forbidden from bundling and which the grading
# host injects at `docker run --gpus` time. Deliberately still a single image
# for both that host and this repo's own GPU-less dev machines: a CUDA-build
# torch imports and runs fine with zero GPU present (`torch.cuda.is_available()`
# simply returns False and every op runs on CPU) -- see app/domain/gpu.py for
# the runtime detection, the shared-GPU memory-fraction cap, and startup
# observability, and semantic_similarity.py for the only caller today.
RUN pip install --index-url https://download.pytorch.org/whl/cu121 \
      torch==2.2.2+cu121

# Промпт 8.2 (CASE10_AGENT_PROMPTS_BACKLOG.md): PaddleOCR as an alternative,
# switchable full-page OCR-fallback engine (app/domain/ocr_paddle.py), evaluated against
# tesseract on the OCR pilot GOLD sample -- see evaluation/OCR_PADDLEOCR_PILOT_REPORT.md
# for the full accuracy comparison and the accept/reject decision (REJECTED as the
# default: no statistically significant accuracy gain over tesseract, overlapping 95%
# Wilson CIs -- see that report). CPU `paddlepaddle` deliberately, NOT `paddlepaddle-gpu`,
# even though CASE10_OCR_ENGINE=paddleocr also runs fine on the grading server's H100 --
# real, confirmed constraint found while building this image: `paddlepaddle-gpu`'s
# CUDA-12.6-era pinned `nvidia-cudnn-cu12`/`nvidia-cublas-cu12`/etc. pip deps silently
# overwrite the CUDA-12.1-era ones the `torch` install above needs, breaking `import
# torch` outright (`ImportError: libcudnn.so.8: cannot open shared object file`) --
# confirmed by actually running the built image, not just a version-pin theory. That
# would silently kill the semantic-anchor feature (semantic_similarity.py, checkpoint 41)
# every time `CASE10_OCR_ENGINE=paddleocr` was enabled. Plain CPU `paddlepaddle` has no
# CUDA pip deps at all, so it cannot conflict with torch's -- `CASE10_OCR_ENGINE=paddleocr`
# is fully functional in this image, just CPU-only (see the pilot report's timing numbers
# for what that costs vs. GPU). GPU-accelerated PaddleOCR in the SAME image as torch is a
# real, scoped follow-up (likely subprocess/venv isolation for paddle, given the
# conflict), not attempted here since the accuracy verdict alone already rejects making
# it the default.
RUN pip install paddlepaddle==3.3.1

RUN pip install paddleocr==3.7.0

RUN pip install -r requirements.txt

COPY api_service/app ./app
COPY api_service/alembic ./alembic
COPY api_service/alembic.ini ./alembic.ini
COPY shared ./shared
COPY evaluation ./evaluation

ENV PYTHONPATH=/app

# Pre-bake the optional semantic-anchor model into the image at build time
# (see app/domain/semantic_similarity.py) so the running container never
# depends on network access to huggingface.co at request time -- a load
# failure there must never be able to affect inference (the module already
# degrades to a no-op if this step's cache is somehow absent). Uses the same
# HF cache layout `sentence-transformers`/`huggingface_hub` reads at
# runtime, so no extra config is needed to find it.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

# Pre-bake the PaddleOCR models (Промпт 8.2 -- see app/domain/ocr_paddle.py) the same
# way, into PaddleX's own cache (`~/.paddlex/official_models`, `PADDLE_PDX_CACHE_HOME` to
# override) -- `check_paddleocr_health()` both downloads the real det+rec model pair for
# `OCR_PADDLE_LANG` and proves the CPU inference path actually runs (catches a build-time
# regression the same way a broken tesseract flags loudly at startup instead of
# silently). `enable_mkldnn=False` in ocr_paddle.py is why this needs to succeed here,
# not just download files:
# PaddlePaddle 3.3.1's default oneDNN kernel for PP-OCRv5_server_det raises
# `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support
# [pir::ArrayAttribute<pir::DoubleAttribute>]` under the new PIR executor -- confirmed
# 100% reproducible on this project's own CPU inference path; this RUN step is what
# would have caught it before it ever reached the image the grading server runs.
RUN python -c "from app.domain.ocr_paddle import check_paddleocr_health; result = check_paddleocr_health(); print(result); assert result['available'], result"

# Model is already cached in the image (see the RUN steps above) -- forbid
# any network call to huggingface.co at runtime rather than merely avoiding
# one, so a transient network issue can never turn into request latency.
ENV HF_HUB_OFFLINE=1
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
