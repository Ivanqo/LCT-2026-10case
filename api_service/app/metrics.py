from prometheus_client import Counter, Gauge, Histogram

chat_requests_total = Counter("chat_requests_total", "Total chat requests", ["project_id"])
uploads_total = Counter("uploads_total", "Total uploads", ["file_type", "project_id"])
retrieval_time_seconds = Histogram("retrieval_time_seconds", "RAG retrieval time seconds", ["project_id"])

# Case10 OCR fallback observability (evaluation/OCR_GOLD_REVIEW_PASS1_REPORT.md finding #1):
# a broken/incompatible local tesseract binary used to fail every call silently
# (`except (FileNotFoundError, subprocess.TimeoutExpired): return ""`), making the OCR
# fallback indistinguishable from "this page has no text". These make that failure mode
# visible instead of silently degrading `ocr_character_accuracy` on ~55% of hidden-test
# pages (test_hidden_213/page_index.jsonl) that route through the OCR fallback.
ocr_tesseract_available = Gauge(
    "ocr_tesseract_available",
    "1 if the tesseract binary responds to the exact flags dataset_sources.py invokes it with, else 0",
)
ocr_tesseract_calls_total = Counter(
    "ocr_tesseract_calls_total", "Total tesseract subprocess invocations by outcome", ["outcome"],
)
ocr_cmap_corruption_detected_total = Counter(
    "ocr_cmap_corruption_detected_total",
    "Pages whose PDF text layer was detected as broken/missing ToUnicode CMap output", ["resolution"],
)

# PaddleOCR alternative engine (Промпт 8.2, CASE10_AGENT_PROMPTS_BACKLOG.md; see
# app/domain/ocr_paddle.py). Mirrors the tesseract gauges/counters above -- same "never
# degrade silently" rationale: CASE10_OCR_ENGINE=paddleocr falling back to tesseract on a
# load failure must be loud, not an unexplained accuracy drop.
ocr_paddleocr_available = Gauge(
    "ocr_paddleocr_available",
    "1 if the PaddleOCR pipeline for the configured lang/device loaded and passed its health check, else 0",
)
ocr_paddleocr_calls_total = Counter(
    "ocr_paddleocr_calls_total", "Total PaddleOCR inference calls by outcome", ["outcome"],
)

# GPU observability (see app/domain/gpu.py): the grading server provides a shared
# physical H100 per team, but a team's own container can still end up CPU-only if the
# NVIDIA Container Toolkit / `--gpus` wiring is misconfigured for that run -- this makes
# that silent-degradation case visible instead of only showing up as unexplained
# embedding-model latency.
gpu_available = Gauge(
    "gpu_available",
    "1 if torch.cuda.is_available() found a CUDA device at API startup, else 0",
)
