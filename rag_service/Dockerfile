FROM python:3.11-slim

# Для ускорения/стабильности сборки
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Системные зависимости:
# - tesseract-ocr: нужен pytesseract
# - poppler-utils: часто нужен pdfplumber/инструменты для pdf
# - libgl1/libglib2.0-0: частые runtime deps для opencv/ultralytics
RUN set -eux; \
    for attempt in 1 2 3; do \
      apt-get update \
      && apt-get \
        -o Acquire::Retries=5 \
        -o Acquire::http::Pipeline-Depth=0 \
        install -y --no-install-recommends \
          tesseract-ocr \
          tesseract-ocr-rus \
          tesseract-ocr-eng \
          poppler-utils \
          fonts-dejavu-core \
          libgl1 \
          libglib2.0-0 \
      && break; \
      if [ "$attempt" = "3" ]; then exit 1; fi; \
      apt-get clean; \
      rm -rf /var/lib/apt/lists/*; \
      sleep 5; \
    done; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY rag_service/requirements.txt ./requirements.txt

# PyTorch ставим CPU-only из официального индекса.
# Это резко уменьшает размер/время сборки и делает её предсказуемой на Ubuntu.
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
      torch==2.2.2 \
      torchvision==0.17.2

RUN pip install -r requirements.txt

COPY rag_service/app ./app
COPY shared ./shared

ENV PYTHONPATH=/app
EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
