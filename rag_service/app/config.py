from __future__ import annotations

import os
from pathlib import Path
from pydantic import BaseModel


class Settings(BaseModel):
    PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

    DATA_DIR: Path = Path(os.getenv("RAG_DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()
    UPLOADS_DIR: Path = Path(os.getenv("RAG_UPLOADS_DIR", str(DATA_DIR / "uploads"))).resolve()
    VECTORSTORE_DIR: Path = Path(os.getenv("RAG_VECTORSTORE_DIR", str(DATA_DIR / "vectorstore"))).resolve()
    LOG_DIR: Path = Path(os.getenv("RAG_LOG_DIR", str(PROJECT_ROOT / "logs"))).resolve()

    # Layout / assets storage
    PAGES_DIR: Path = Path(os.getenv("RAG_PAGES_DIR", str(DATA_DIR / "pages"))).resolve()
    REGIONS_DIR: Path = Path(os.getenv("RAG_REGIONS_DIR", str(DATA_DIR / "regions"))).resolve()

    SQLITE_PATH: Path = Path(os.getenv("RAG_SQLITE_PATH", str(DATA_DIR / "rag.db"))).resolve()
    SQLALCHEMY_DATABASE_URL: str = os.getenv("RAG_DATABASE_URL", f"sqlite:///{SQLITE_PATH}")

    # Chunking
    CHUNK_SIZE: int = int(os.getenv("RAG_CHUNK_SIZE", "1400"))
    CHUNK_OVERLAP: int = int(os.getenv("RAG_CHUNK_OVERLAP", "220"))
    CHUNK_MIN_CHARS: int = int(os.getenv("RAG_CHUNK_MIN_CHARS", "160"))

    # Embeddings — multilingual by default to support Russian documentation.
    EMBEDDING_MODEL: str = os.getenv(
        "RAG_EMBEDDING_MODEL",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    COLLECTION_NAME: str = os.getenv("RAG_COLLECTION_NAME", "chunks_v2")
    COLLECTION_TEXT: str = os.getenv("RAG_COLLECTION_TEXT", COLLECTION_NAME)
    COLLECTION_ASSET: str = os.getenv("RAG_COLLECTION_ASSET", "assets_v2")

    # Layout extraction
    RENDER_DPI: int = int(os.getenv("RAG_RENDER_DPI", "120"))
    RENDER_MAX_SIDE: int = int(os.getenv("RAG_RENDER_MAX_SIDE", "2400"))
    OCR_LANG: str = os.getenv("RAG_OCR_LANG", "rus+eng")
    ENABLE_OCR: bool = os.getenv("RAG_ENABLE_OCR", "0") not in ("0", "false", "False", "no", "NO")
    ENABLE_OCR_LARGE_REGIONS: bool = os.getenv("RAG_ENABLE_OCR_LARGE_REGIONS", "0") in ("1", "true", "True", "yes", "YES")

    # OCR tuning
    OCR_TIMEOUT_SEC: int = int(os.getenv("RAG_OCR_TIMEOUT_SEC", "20"))
    OCR_FAST_MAX_SIDE: int = int(os.getenv("RAG_OCR_FAST_MAX_SIDE", "1600"))
    OCR_WORD_LEVEL_TYPES: str = os.getenv("RAG_OCR_WORD_LEVEL_TYPES", "title_block,table")
    ENABLE_TABLE_DETECTION: bool = os.getenv("RAG_ENABLE_TABLE_DETECT", os.getenv("RAG_ENABLE_TABLE_DETECTION", "0")) in (
        "1",
        "true",
        "True",
        "yes",
        "YES",
    )
    LAYOUT_DETECTOR: str = os.getenv("RAG_LAYOUT_DETECTOR", "yolo")
    YOLO_MODEL_PATH: Path = Path(os.getenv("RAG_YOLO_MODEL_PATH", str(DATA_DIR / "models" / "yolo-doclaynet.pt"))).resolve()
    YOLO_CONF: float = float(os.getenv("RAG_YOLO_CONF", "0.25"))
    YOLO_IOU: float = float(os.getenv("RAG_YOLO_IOU", "0.45"))
    YOLO_MAX_DET: int = int(os.getenv("RAG_YOLO_MAX_DET", "50"))
    YOLO_DEVICE: str = os.getenv("RAG_YOLO_DEVICE", "")

    MAX_PAGES: int = int(os.getenv("RAG_MAX_PAGES", "0"))
    EMBED_BATCH_SIZE: int = int(os.getenv("RAG_EMBED_BATCH_SIZE", "32"))
    ENABLE_VECTOR_SEARCH: bool = os.getenv("RAG_ENABLE_VECTOR_SEARCH", "0") in ("1", "true", "True", "yes", "YES")
    ENABLE_ASSET_SEARCH: bool = os.getenv("RAG_ENABLE_ASSET_SEARCH", "0") in ("1", "true", "True", "yes", "YES")
    ENABLE_LLM_SUMMARY: bool = os.getenv("RAG_ENABLE_LLM_SUMMARY", "0") in ("1", "true", "True", "yes", "YES")
    INDEX_EMBEDDINGS: bool = os.getenv("RAG_INDEX_EMBEDDINGS", os.getenv("RAG_ENABLE_VECTOR_SEARCH", "0")) in (
        "1",
        "true",
        "True",
        "yes",
        "YES",
    )

    # LLM
    QWEN_MODEL: str = os.getenv("QWEN_MODEL", "qwen3.7-max")
    SYSTEM_PROMPT: str = os.getenv(
        "RAG_SYSTEM_PROMPT",
        "Ты — инженерный ассистент по проектной документации. Отвечай только по найденным данным из документов. Пиши простым профессиональным русским языком: сначала прямой вывод, затем где это найдено в документации, затем важные замечания или ограничения. Никогда не показывай пользователю служебные идентификаторы, слова chunk, region, asset, embedding, retriever, score и внутренние технические детали.",
    )


settings = Settings()
for p in [settings.DATA_DIR, settings.UPLOADS_DIR, settings.VECTORSTORE_DIR, settings.LOG_DIR, settings.PAGES_DIR, settings.REGIONS_DIR]:
    p.mkdir(parents=True, exist_ok=True)
