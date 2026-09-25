from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional, Sequence

import chromadb
from chromadb.config import Settings as ChromaSettings
from sentence_transformers import SentenceTransformer

from .config import settings

logger = logging.getLogger(__name__)

_VECTORSTORES: Optional["VectorStores"] = None


class VectorStores:
    """Multiple Chroma collections with a shared embedder."""

    def __init__(self, *, persist_dir: str, embedding_model: str):
        client = chromadb.PersistentClient(
            path=persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.collection_text = client.get_or_create_collection(name=settings.COLLECTION_TEXT)
        self.collection_asset = client.get_or_create_collection(name=settings.COLLECTION_ASSET)

        self.embedding_model = embedding_model
        self._embedder: SentenceTransformer | None = None

        logger.info(
            "VectorStores initialized: dir=%s text=%s asset=%s model=%s",
            persist_dir,
            settings.COLLECTION_TEXT,
            settings.COLLECTION_ASSET,
            embedding_model,
        )

    def _get_embedder(self) -> SentenceTransformer:
        if self._embedder is None:
            self._embedder = SentenceTransformer(self.embedding_model)
            logger.info("Embedding model loaded: %s", self.embedding_model)
        return self._embedder

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        rows = list(texts)
        batch_size = max(1, int(getattr(settings, "EMBED_BATCH_SIZE", 32) or 32))
        out: List[List[float]] = []
        embedder = self._get_embedder()
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            vectors = embedder.encode(batch, normalize_embeddings=True)
            out.extend(v.tolist() for v in vectors)
        return out

    def upsert_text(
        self,
        *,
        ids: Sequence[str],
        texts: Sequence[str],
        metadatas: Sequence[Dict[str, Any]],
    ) -> None:
        _validate_lengths(ids=ids, texts=texts, metadatas=metadatas)
        embeddings = self.embed(texts)
        metadatas_clean = [_sanitize_metadata(m) for m in metadatas]
        self.collection_text.upsert(
            ids=list(ids),
            documents=list(texts),
            metadatas=list(metadatas_clean),
            embeddings=embeddings,
        )

    def upsert_asset(
        self,
        *,
        ids: Sequence[str],
        texts: Sequence[str],
        metadatas: Sequence[Dict[str, Any]],
    ) -> None:
        _validate_lengths(ids=ids, texts=texts, metadatas=metadatas)
        embeddings = self.embed(texts)
        metadatas_clean = [_sanitize_metadata(m) for m in metadatas]
        self.collection_asset.upsert(
            ids=list(ids),
            documents=list(texts),
            metadatas=list(metadatas_clean),
            embeddings=embeddings,
        )

    def query_text(self, *, query_text: str, top_k: int, where: Dict[str, Any]) -> List[Dict[str, Any]]:
        q_emb = self.embed([query_text])[0]
        include = _safe_include(["documents", "metadatas", "distances"])
        kwargs: Dict[str, Any] = {}
        if include:
            kwargs["include"] = include
        chroma_where = _normalize_where(where)
        res = self.collection_text.query(query_embeddings=[q_emb], n_results=top_k, where=chroma_where, **kwargs)
        return _unpack(res)

    def query_asset(self, *, query_text: str, top_k: int, where: Dict[str, Any]) -> List[Dict[str, Any]]:
        q_emb = self.embed([query_text])[0]
        include = _safe_include(["documents", "metadatas", "distances"])
        kwargs: Dict[str, Any] = {}
        if include:
            kwargs["include"] = include
        chroma_where = _normalize_where(where)
        res = self.collection_asset.query(query_embeddings=[q_emb], n_results=top_k, where=chroma_where, **kwargs)
        return _unpack(res)

    def delete_text(self, ids: Sequence[str]) -> int:
        ids_clean = [str(x) for x in ids if str(x).strip()]
        if not ids_clean:
            return 0
        self.collection_text.delete(ids=ids_clean)
        return len(ids_clean)

    def delete_asset(self, ids: Sequence[str]) -> int:
        ids_clean = [str(x) for x in ids if str(x).strip()]
        if not ids_clean:
            return 0
        self.collection_asset.delete(ids=ids_clean)
        return len(ids_clean)

    def delete_text_where(self, where: Dict[str, Any]) -> None:
        self.collection_text.delete(where=_normalize_where(where))

    def delete_asset_where(self, where: Dict[str, Any]) -> None:
        self.collection_asset.delete(where=_normalize_where(where))


def _normalize_where(where: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a Chroma-compatible filter.

    Some Chroma versions reject plain multi-key dicts and require a single top-level
    operator, e.g. {"$and": [{"project_id": 3}, {"organization_id": 1}]}.
    """
    if not where:
        return {}
    if any(str(k).startswith("$") for k in where):
        return where
    items = [{k: v} for k, v in where.items()]
    if len(items) == 1:
        return items[0]
    return {"$and": items}


def _unpack(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize Chroma query response into a flat list.

    Note: Chroma returns ids even if they are not requested via `include`.
    """

    out: List[Dict[str, Any]] = []
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    for i in range(len(ids)):
        out.append(
            {
                "id": ids[i],
                "text": docs[i] if i < len(docs) else None,
                "meta": metas[i] or {} if i < len(metas) else {},
                "distance": dists[i] if i < len(dists) else None,
            }
        )
    return out


def _safe_include(items: Sequence[str]) -> List[str]:
    """Filter `include` for Chroma queries.

    Chroma validates `include` strictly, and the allowed set differs between versions.
    Crucially: **do not pass `ids`** here (ids are returned regardless).
    """

    allowed = {"embeddings", "documents", "metadatas", "uris", "data", "distances"}
    out = [x for x in items if x in allowed]
    dropped = [x for x in items if x not in allowed]
    if dropped:
        logger.warning("Dropped unsupported Chroma include items: %s", dropped)
    return out


def _validate_lengths(*, ids: Sequence[str], texts: Sequence[str], metadatas: Sequence[Dict[str, Any]]) -> None:
    if not (len(ids) == len(texts) == len(metadatas)):
        raise ValueError(
            f"Length mismatch: ids={len(ids)} texts={len(texts)} metadatas={len(metadatas)}"
        )


def _sanitize_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Chroma metadata values must be: str, int, float, bool.

    This prevents ingestion crashes like:
    `ValueError: Expected metadata value to be a str, int, float or bool, got None`.

    Strategy:
    - drop keys with None
    - drop NaN/inf floats
    - decode bytes
    - cast numpy/torch scalars via `.item()` when possible
    - serialize complex objects to JSON strings (fallback to str)
    """

    clean: Dict[str, Any] = {}
    for k, v in (meta or {}).items():
        if v is None:
            continue

        # numpy/torch scalars, etc.
        try:
            if hasattr(v, "item") and callable(getattr(v, "item")):
                v = v.item()  # type: ignore[assignment]
        except Exception:
            pass

        if isinstance(v, (str, bool, int)):
            clean[k] = v
            continue

        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                continue
            clean[k] = v
            continue

        if isinstance(v, (bytes, bytearray)):
            try:
                clean[k] = v.decode("utf-8", errors="replace")
            except Exception:
                clean[k] = str(v)
            continue

        try:
            clean[k] = json.dumps(v, ensure_ascii=False)
        except Exception:
            clean[k] = str(v)

    return clean


def get_vectorstores() -> VectorStores:
    global _VECTORSTORES
    if _VECTORSTORES is None:
        _VECTORSTORES = VectorStores(
            persist_dir=str(settings.VECTORSTORE_DIR),
            embedding_model=settings.EMBEDDING_MODEL,
        )
    return _VECTORSTORES


class LazyVectorStores:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_vectorstores(), name)


# Backward-compatible alias
def get_vectorstore():
    return get_vectorstores()
