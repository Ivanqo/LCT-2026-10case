# CASE10 API / worker / CLI image. One dependency stage, two targets:
#
#   delivery (default: the last stage) -- the offline CASE10 delivery: everything the inspection pipeline needs
#            and nothing else. No external-service client code (Qwen proxy / chat), no PaddleOCR (evaluated and
#            rejected as the default OCR engine: evaluation/OCR_PADDLEOCR_PILOT_REPORT.md), no organizer data, no
#            internal reports. The embedding model is baked in and HF_HUB_OFFLINE=1, so the container runs with
#            `--network none` (README_GRADER.md, LICENSES_MODELS.md).
#   dev      -- the same dependencies plus the legacy pieces the development compose still runs (chat through the
#            Qwen proxy, RAG/IFC services, the optional PaddleOCR engine). `docker-compose.yml` builds this target.
#
# Digest-pinned (not just tag-pinned): the apt archive snapshot a Debian base image
# resolves to moves with the image, so pinning only the tesseract-ocr package version
# below is not by itself reproducible -- a later `python:3.11-slim` pull can land on a
# newer Debian snapshot where that exact package build no longer exists, breaking the
# build outright. Pinning the digest freezes the apt snapshot these versions were
# verified against (2026-09-20: Debian 13 "trixie", resolves the tesseract-ocr --psm
# double-dash-flag incompatibility found when auditing OCR fallback observability --
# see evaluation/OCR_GOLD_REVIEW_PASS1_REPORT.md finding #1).
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9 AS deps

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
    && rm -rf /var/lib/apt/lists/*
COPY api_service/requirements.txt ./requirements.txt

# PyTorch built against CUDA 12.1 (Hopper/sm_90 kernels included since
# PyTorch 2.1 -- covers the NVIDIA H100 the organizer's grading server passes
# into each team's container via the NVIDIA Container Toolkit; CUDA >=12.0 /
# compute capability 9.0 per the organizer's confirmed spec). This wheel pulls
# in the CUDA *runtime* shared libraries as ordinary pip deps (nvidia-cublas-
# cu12, nvidia-cudnn-cu12, ...) -- never the NVIDIA driver itself, which
# participants are explicitly forbidden from bundling and which the grading
# host injects at `docker run --gpus` time. Deliberately still a single image
# for both that host and GPU-less machines: a CUDA-build torch imports and runs
# fine with zero GPU present (`torch.cuda.is_available()` simply returns False
# and every op runs on CPU) -- see app/domain/gpu.py for the runtime detection,
# the shared-GPU memory-fraction cap, and startup observability, and
# semantic_similarity.py for the only caller today.
RUN pip install --index-url https://download.pytorch.org/whl/cu121 \
      torch==2.2.2+cu121

RUN pip install -r requirements.txt

# Pre-bake the semantic-anchor model at build time (app/domain/semantic_similarity.py)
# into the HF cache layout `sentence-transformers` reads at runtime, so the running
# container never depends on huggingface.co. Before any code COPY, so a code change does
# not re-download the model.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

# Model is already cached in the image -- forbid any network call to huggingface.co at
# runtime rather than merely avoiding one.
ENV PYTHONPATH=/app \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# ======================================================================== dev
FROM deps AS dev

# libgl1/libglib2.0-0: opencv-python (a transitive paddleocr/paddlex dependency,
# Промпт 8.2) dynamically loads libGL.so.1 at import time even though this is a headless
# server image with no display -- confirmed real (`ImportError: libGL.so.1: cannot open
# shared object file`) on the very first build attempt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Промпт 8.2: PaddleOCR as an alternative, switchable full-page OCR-fallback engine
# (app/domain/ocr_paddle.py; CASE10_OCR_ENGINE=paddleocr), rejected as the default --
# no statistically significant accuracy gain over tesseract, overlapping 95% Wilson CIs
# (evaluation/OCR_PADDLEOCR_PILOT_REPORT.md). CPU `paddlepaddle` deliberately, NOT
# `paddlepaddle-gpu`: the latter's CUDA-12.6-era pinned nvidia-* pip deps overwrite the
# CUDA-12.1-era ones torch needs and break `import torch` (confirmed on a built image).
# requirements.txt is re-applied afterwards so its pins (numpy 1.26.4 for torch 2.2.2's
# ABI) win over whatever the paddle stack resolved.
RUN pip install paddlepaddle==3.3.1 \
    && pip install paddleocr==3.7.0 \
    && pip install -r requirements.txt

COPY api_service/app ./app
COPY api_service/alembic ./alembic
COPY api_service/alembic.ini ./alembic.ini
COPY evaluation/__init__.py evaluation/metrics.py evaluation/exporter.py evaluation/fixtures.py \
     evaluation/candidate_coverage.py ./evaluation/

# Pre-bake the PaddleOCR det+rec models and prove the CPU inference path runs (a broken
# build fails here instead of at the first request). `enable_mkldnn=False` in
# ocr_paddle.py is why this must succeed, not just download files: PaddlePaddle 3.3.1's
# default oneDNN kernel for PP-OCRv5_server_det raises NotImplementedError under PIR.
RUN HF_HUB_OFFLINE=0 python -c "from app.domain.ocr_paddle import check_paddleocr_health; result = check_paddleocr_health(); print(result); assert result['available'], result"

# Qwen-proxy client used by the legacy chat routes only (never by the CASE10 pipeline).
COPY shared ./shared
ENV CASE10_OFFLINE_DELIVERY=0


# =================================================================== delivery
FROM deps AS delivery

COPY api_service/app ./app
COPY api_service/alembic ./alembic
COPY api_service/alembic.ini ./alembic.ini
# Only the runtime part of `evaluation/` (the submission exporter and what it imports): the rest of that folder is
# measurement code, internal reports and labels, which do not belong in the delivered image.
COPY evaluation/__init__.py evaluation/metrics.py evaluation/exporter.py evaluation/fixtures.py \
     evaluation/candidate_coverage.py ./evaluation/

# Offline profile (app/config.py): legacy chat/RAG routers are not mounted, RAG/IFC/Qwen clients refuse to connect.
# The parameter matrix and submission schema come from the reference data shipped with the code. Server-side
# packages (POST /api/case10/batch-runs) live below /data/packages, which is also where the worker reads document
# bytes from; `python -m app.cli run` sets its own originals root per run. No queue/cache server by default: the
# stand compose sets RABBITMQ_URL for the api/worker pair.
ENV CASE10_OFFLINE_DELIVERY=1 \
    CASE10_DATASET_ROOT=/app/app/reference_data/case_data \
    CASE10_BATCH_PACKAGES_ROOT=/data/packages \
    CASE10_ORIGINALS_ROOT=/data/packages \
    API_DATA_DIR=/data \
    API_LOG_DIR=/logs \
    RABBITMQ_URL= \
    REDIS_URL=
