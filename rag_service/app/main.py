from __future__ import annotations

import ast
import hashlib
import json
import logging
import math
import re
import shutil
import threading
import unicodedata
import uuid
from io import BytesIO
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import or_, select
from PIL import Image

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None
from sqlalchemy.orm import Session

from .config import settings
from .db.models import Document, Page, Region, Chunk
from .db.session import init_db, get_db
from .ingestion import ingest_file, reindex_document_text
from .layout_ingestion import ingest_pdf_layout
from .llm_structured import ask_structured_answer
from .logging_setup import setup_logging
from .schemas import (
    IngestResponse,
    LayoutIngestResponse,
    AskRequest,
    AskResponse,
    SourceChunk,
    SourceAsset,
    DocumentItem,
    PageItem,
    RegionItem,
    SourceFragmentExportItem,
    AssetSearchRequest,
    AssetSearchResponse,
    AssetSearchHit,
    DeleteDocumentResponse,
    ReindexDocumentResponse,
    PageViewerItem,
)
from .source_selection import select_visible_sources
from .vectorstore import LazyVectorStores, get_vectorstores

setup_logging("rag")
logger = logging.getLogger(__name__)
app = FastAPI(title="RAG Service", version="2.4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

UPLOAD_CHUNK_SIZE = 1024 * 1024
_INDEX_LOCK = threading.Lock()


REGION_TYPE_LABELS = {
    "table": "Таблица",
    "drawing": "Чертёж",
    "page": "Страница",
    "title_block": "Основная надпись",
    "text": "Текстовый блок",
}
REGION_PRIORITY = {"table": 0, "drawing": 1, "title_block": 2, "page": 3}


_TOKEN_RX = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+(?:[-_/.][A-Za-zА-Яа-яЁё0-9]+)*")
_STOPWORDS = {
    "и", "в", "во", "на", "по", "с", "со", "к", "ко", "у", "из", "за", "для", "о", "об", "от", "до", "под",
    "как", "что", "это", "ли", "или", "а", "но", "не", "над", "при", "про", "из", "без", "по", "стр", "страница",
    "где", "какой", "какая", "какие", "какое", "который", "которая", "которые", "есть", "нет", "нужно", "надо",
    "найди", "покажи", "скажи", "расскажи", "документ", "документы", "документация", "проект", "проектный",
    "the", "and", "for", "with", "from", "page", "file", "document", "project",
}
_RU_ENDINGS = (
    "иями", "ями", "ами", "его", "ого", "ему", "ому", "ими", "ыми", "ией", "ия", "ья", "ье", "ью",
    "ах", "ях", "ов", "ев", "ей", "ой", "ый", "ий", "ая", "яя", "ое", "ее", "ую", "юю", "ам", "ям",
    "ом", "ем", "ым", "им", "ых", "их", "ою", "ею", "ы", "и", "а", "я", "о", "е", "у", "ю",
)


def _normalize_search_text(text: object) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower().replace("ё", "е")
    return re.sub(r"\s+", " ", value).strip()


def _normalize_token(token: str) -> str:
    return _normalize_search_text(token)


def _russian_stem(token: str) -> str:
    token = _normalize_token(token)
    if not re.search(r"[а-я]", token):
        return token
    for ending in _RU_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[: -len(ending)]
    return token


def _token_variants(token: str) -> set[str]:
    token = _normalize_token(token)
    variants = {token}
    stem = _russian_stem(token)
    if len(stem) >= 4:
        variants.add(stem)
    return variants


def _tokenize_query(text: str) -> list[str]:
    raw = [_normalize_token(t) for t in _TOKEN_RX.findall(_normalize_search_text(text))]
    out: list[str] = []
    seen: set[str] = set()
    for token in raw:
        if len(token) < 2:
            continue
        if token in _STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _search_terms(tokens: list[str]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for variant in sorted(_token_variants(token), key=lambda x: (-len(x), x)):
            if len(variant) < 3 or variant in _STOPWORDS or variant in seen:
                continue
            seen.add(variant)
            terms.append(variant)
    return terms


def _token_weight(token: str) -> float:
    weight = 1.0
    if any(ch.isdigit() for ch in token):
        weight += 0.9
    if any(ch in token for ch in "-_/"):
        weight += 0.6
    if len(token) >= 8:
        weight += 0.2
    return weight


def _normalize_search_blob(*parts: object) -> str:
    text = " ".join(str(p or "") for p in parts)
    return _normalize_search_text(text)


def _query_ngrams(tokens: list[str], max_n: int = 4) -> list[str]:
    grams: list[str] = []
    for n in range(min(max_n, len(tokens)), 1, -1):
        for i in range(0, len(tokens) - n + 1):
            grams.append(" ".join(tokens[i:i+n]))
    return grams


def _keyword_candidates_from_db(db: Session, *, project_id: int, organization_id: int | None, question: str, limit: int) -> list[dict]:
    tokens = _tokenize_query(question)
    if not tokens:
        return []

    focus = sorted(_search_terms(tokens), key=lambda t: (-_token_weight(t), -len(t)))[:12]
    clauses = []
    for phrase in _query_ngrams(tokens, max_n=4)[:6]:
        like = f"%{phrase}%"
        clauses.append(Chunk.text.ilike(like))
        clauses.append(Document.filename.ilike(like))
    for token in focus:
        like = f"%{token}%"
        clauses.append(Chunk.text.ilike(like))
        clauses.append(Document.filename.ilike(like))

    if not clauses:
        return []

    candidate_limit = min(max(limit * 40, 500), 5000)
    rows = db.execute(
        select(Chunk, Document)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Chunk.project_id == project_id,
            Chunk.organization_id == organization_id,
            or_(*clauses),
        )
        .order_by(Chunk.id.asc())
        .limit(candidate_limit)
    ).all()

    out: list[dict] = []
    for chunk, document in rows:
        out.append(
            {
                "id": str(chunk.embedding_id or chunk.id),
                "text": str(chunk.text or ""),
                "meta": {
                    "chunk_id": int(chunk.id),
                    "document_id": int(chunk.document_id),
                    "organization_id": int(chunk.organization_id or 0),
                    "project_id": int(chunk.project_id),
                    "page_number": int(chunk.page_number or 0),
                    "filename": str(document.filename or ""),
                    "region_id": int(chunk.region_id) if chunk.region_id is not None else None,
                    "region_type": "chunk",
                },
                "distance": None,
                "_lexical_only": True,
            }
        )
    return out


def _score_text_hit(question: str, hit: dict) -> tuple[float, dict]:
    meta = hit.get("meta") or {}
    text = str(hit.get("text") or "")
    filename = str(meta.get("filename") or "")
    haystack = _normalize_search_blob(text, filename)
    q_norm = _normalize_search_blob(question)
    query_tokens = _tokenize_query(question)
    hay_tokens = _tokenize_query(haystack)
    hay_terms = set(_search_terms(hay_tokens))

    semantic_distance = hit.get("distance")
    semantic_score = 0.0
    if semantic_distance is not None:
        try:
            semantic_score = 1.0 / (1.0 + max(0.0, float(semantic_distance)))
        except Exception:
            semantic_score = 0.0

    matched_weight = 0.0
    total_weight = 0.0
    strong_matches = 0
    matched_terms: list[str] = []
    for token in query_tokens:
        weight = _token_weight(token)
        total_weight += weight
        variants = _token_variants(token)
        matched = bool(variants & hay_terms)
        if not matched and (any(ch.isdigit() for ch in token) or any(ch in token for ch in "-_/")):
            matched = any(variant in haystack for variant in variants)
        if matched:
            matched_weight += weight
            matched_terms.append(token)
            if weight >= 1.6:
                strong_matches += 1
    lexical_score = matched_weight / total_weight if total_weight else 0.0

    phrase_bonus = 0.0
    if q_norm and len(q_norm) >= 8 and q_norm in haystack:
        phrase_bonus += 0.34
    for phrase in _query_ngrams(query_tokens, max_n=4)[:8]:
        if len(phrase) >= 6 and phrase in haystack:
            phrase_bonus += 0.08 + min(0.12, len(phrase.split()) * 0.03)

    filename_bonus = 0.0
    filename_lower = _normalize_search_text(filename)
    for token in query_tokens:
        if any(variant in filename_lower for variant in _token_variants(token)):
            filename_bonus += 0.08 if _token_weight(token) < 1.6 else 0.16

    density_bonus = min(0.18, strong_matches * 0.05)
    text_len = max(1, len(text))
    brevity_penalty = 0.0
    if text_len < 80:
        brevity_penalty = 0.08

    exact_heading_bonus = 0.0
    compact_text = re.sub(r"\s+", " ", text).strip()
    if compact_text and len(compact_text) <= 180:
        for phrase in _query_ngrams(query_tokens, max_n=5)[:10]:
            if len(phrase) >= 8 and phrase in compact_text.lower():
                exact_heading_bonus += 0.12
                break

    final_score = semantic_score * 0.32 + lexical_score * 0.62 + phrase_bonus * 1.2 + filename_bonus + density_bonus + exact_heading_bonus - brevity_penalty
    info = {
        "semantic_score": semantic_score,
        "lexical_score": lexical_score,
        "matched_terms": matched_terms,
        "final_score": final_score,
    }
    return final_score, info


def _rerank_text_hits(question: str, hits: list[dict], *, limit: int) -> list[dict]:
    best_by_chunk: dict[int, dict] = {}
    for hit in hits:
        meta = hit.get("meta") or {}
        chunk_id_raw = meta.get("chunk_id")
        try:
            chunk_id = int(chunk_id_raw)
        except Exception:
            continue
        score, info = _score_text_hit(question, hit)
        enriched = dict(hit)
        enriched["_rank"] = score
        enriched["_rank_info"] = info
        current = best_by_chunk.get(chunk_id)
        if current is None or score > float(current.get("_rank") or -999):
            best_by_chunk[chunk_id] = enriched

    ranked = sorted(
        best_by_chunk.values(),
        key=lambda h: (-(float(h.get("_rank") or 0.0)), h.get("distance") is None, float(h.get("distance") or 0.0), int((h.get("meta") or {}).get("page_number") or 0)),
    )
    relevant_ranked = []
    for hit in ranked:
        info = hit.get("_rank_info") or {}
        rank = float(hit.get("_rank") or 0.0)
        lexical = float(info.get("lexical_score") or 0.0)
        semantic = float(info.get("semantic_score") or 0.0)
        if rank >= 0.18 or lexical >= 0.25 or semantic >= 0.58:
            relevant_ranked.append(hit)
    if relevant_ranked:
        ranked = relevant_ranked

    diversified: list[dict] = []
    by_page: defaultdict[tuple[int, int], int] = defaultdict(int)
    for hit in ranked:
        meta = hit.get("meta") or {}
        key = (int(meta.get("document_id") or 0), int(meta.get("page_number") or 0))
        if by_page[key] >= 2 and len(diversified) < max(limit, 8):
            continue
        diversified.append(hit)
        by_page[key] += 1
        if len(diversified) >= limit:
            break

    if len(diversified) < min(limit, len(ranked)):
        seen = {int((item.get("meta") or {}).get("chunk_id") or -1) for item in diversified}
        for hit in ranked:
            chunk_id = int((hit.get("meta") or {}).get("chunk_id") or -1)
            if chunk_id in seen:
                continue
            diversified.append(hit)
            seen.add(chunk_id)
            if len(diversified) >= limit:
                break

    return diversified


def _hybrid_query_text(db: Session, *, question: str, project_id: int, organization_id: int | None, top_k: int) -> list[dict]:
    base_where = {"project_id": project_id, "organization_id": int(organization_id or 0)}
    vector_hits: list[dict] = []
    if bool(getattr(settings, "ENABLE_VECTOR_SEARCH", False)):
        vector_limit = min(max(top_k * 12, 30), 100)
        try:
            vector_hits = get_vectorstores().query_text(query_text=question, top_k=vector_limit, where=base_where)
        except Exception as exc:
            logger.warning("vector_text_query_failed project_id=%s organization_id=%s: %s", project_id, organization_id, exc)
            vector_hits = []
    lexical_hits = _keyword_candidates_from_db(
        db,
        project_id=project_id,
        organization_id=organization_id,
        question=question,
        limit=min(max(top_k * 8, 24), 80),
    )
    combined = list(vector_hits) + lexical_hits
    return _rerank_text_hits(question, combined, limit=min(max(top_k * 2, 8), 14))


def org_scope(organization_id: int | None) -> str:
    return f"org_{organization_id or 0}"


async def _store_upload_file(file: UploadFile, *, proj_dir: Path, original_filename: str) -> Path:
    proj_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = proj_dir / f".upload_{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    total = 0
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
                out.write(chunk)
        if total <= 0:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Empty file")
        filename = Path(str(original_filename or "document")).name or "document"
        stored_path = proj_dir / f"{digest.hexdigest()[:16]}_{filename}"
        tmp_path.replace(stored_path)
        return stored_path
    except HTTPException:
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _run_index_job(func, *args, **kwargs):
    with _INDEX_LOCK:
        return func(*args, **kwargs)


@app.on_event("startup")
def _startup():
    # Keep startup lightweight: do not eagerly load the embedding model here.
    # Otherwise the container may stay unavailable for a long time and API calls
    # from the frontend will fail with ConnectError before RAG is ready.
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ingest", response_model=IngestResponse)
async def ingest(
    project_id: int = Form(...),
    organization_id: int | None = Form(default=None),
    file_type: str = Form(...),
    original_filename: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if file_type not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="RAG service supports only pdf|docx")
    proj_dir = settings.UPLOADS_DIR / org_scope(organization_id) / str(project_id)
    stored_path = await _store_upload_file(file, proj_dir=proj_dir, original_filename=original_filename)
    doc_id, chunks_created = await run_in_threadpool(
        _run_index_job,
        ingest_file,
        db=db,
        vectorstores=LazyVectorStores(),
        project_id=int(project_id),
        organization_id=organization_id,
        file_type=file_type,
        stored_path=stored_path,
        original_filename=original_filename,
    )
    return IngestResponse(document_id=doc_id, chunks_created=chunks_created)


@app.post("/ingest_layout", response_model=LayoutIngestResponse)
async def ingest_layout(
    project_id: int = Form(...),
    organization_id: int | None = Form(default=None),
    original_filename: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    proj_dir = settings.UPLOADS_DIR / org_scope(organization_id) / str(project_id)
    stored_path = await _store_upload_file(file, proj_dir=proj_dir, original_filename=original_filename)
    doc_id, pages_created, regions_created, text_chunks_created, asset_records_created = await run_in_threadpool(
        _run_index_job,
        ingest_pdf_layout,
        db=db,
        vectorstores=LazyVectorStores(),
        project_id=int(project_id),
        organization_id=organization_id,
        stored_path=stored_path,
        original_filename=original_filename,
    )
    return LayoutIngestResponse(
        document_id=doc_id,
        pages_created=pages_created,
        regions_created=regions_created,
        text_chunks_created=text_chunks_created,
        asset_records_created=asset_records_created,
    )


@app.get("/documents", response_model=list[DocumentItem])
def documents(project_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Document)
        .where(Document.project_id == project_id, Document.organization_id == organization_id)
        .order_by(Document.uploaded_at.desc())
    ).scalars().all()
    items = []
    for row in rows:
        status = str(getattr(row, "status", None) or "ready")
        stage = getattr(row, "stage", None)
        try:
            progress = int(getattr(row, "progress", 100) or 0)
        except Exception:
            progress = 0
        if (status in {"", "processing"} and str(stage or "").startswith("Документ готов")) or status == "ready":
            status = "ready"
            if progress == 0:
                progress = 100
        items.append(DocumentItem(id=row.id, project_id=row.project_id, filename=row.filename, uploaded_at=row.uploaded_at, source_type="rag", status=status, stage=stage, progress=progress, detail=getattr(row, "detail", None)))
    return items


@app.get("/documents/{document_id}/download")
def download_document(document_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    doc = db.execute(select(Document).where(Document.id == document_id, Document.organization_id == organization_id)).scalar_one_or_none()
    if not doc or not doc.stored_path or not Path(doc.stored_path).exists():
        raise HTTPException(status_code=404, detail="Document not found")
    media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if str(doc.filename).lower().endswith(".docx") else "application/pdf" if str(doc.filename).lower().endswith(".pdf") else "application/octet-stream"
    return FileResponse(doc.stored_path, media_type=media_type, filename=str(doc.filename or Path(doc.stored_path).name))


@app.post("/documents/{document_id}/reindex", response_model=ReindexDocumentResponse)
def reindex_document(document_id: int, project_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    try:
        doc_id, pages_created, chunks_created, old_chunks_deleted = _run_index_job(
            reindex_document_text,
            db=db,
            vectorstores=LazyVectorStores(),
            document_id=int(document_id),
            project_id=int(project_id),
            organization_id=organization_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Document reindex failed: {exc}") from exc
    return ReindexDocumentResponse(
        document_id=doc_id,
        pages_created=pages_created,
        chunks_created=chunks_created,
        old_chunks_deleted=old_chunks_deleted,
    )


def _safe_remove_path(path_value: str | None) -> bool:
    if not path_value:
        return False
    try:
        path = Path(path_value)
        if path.is_file():
            path.unlink(missing_ok=True)
            return True
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return True
    except Exception:
        return False
    return False


def _cleanup_empty_parents(path_value: str | None, stop_at: Path) -> None:
    if not path_value:
        return
    try:
        path = Path(path_value).parent if Path(path_value).suffix else Path(path_value)
        stop_at = stop_at.resolve()
        while path.exists() and path != stop_at and stop_at in path.resolve().parents:
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent
    except Exception:
        return


def _delete_chroma_ids(*, text_ids: list[str], asset_ids: list[str]) -> tuple[int, int]:
    deleted_text = 0
    deleted_asset = 0
    try:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        client = chromadb.PersistentClient(
            path=str(settings.VECTORSTORE_DIR),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        if text_ids:
            client.get_or_create_collection(name=settings.COLLECTION_TEXT).delete(ids=text_ids)
            deleted_text = len(text_ids)
        if asset_ids:
            client.get_or_create_collection(name=settings.COLLECTION_ASSET).delete(ids=asset_ids)
            deleted_asset = len(asset_ids)
    except Exception:
        logger.info("Chroma delete skipped/failed for document cleanup", exc_info=True)
    return deleted_text, deleted_asset


@app.delete("/documents/{document_id}", response_model=DeleteDocumentResponse)
def delete_document(document_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    doc = db.execute(select(Document).where(Document.id == document_id, Document.organization_id == organization_id)).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    chunks = db.execute(select(Chunk).where(Chunk.document_id == doc.id)).scalars().all()
    pages = db.execute(select(Page).where(Page.document_id == doc.id)).scalars().all()
    page_ids = [p.id for p in pages]
    regions = db.execute(select(Region).where(Region.page_id.in_(page_ids))).scalars().all() if page_ids else []

    text_ids = [str(ch.embedding_id or ch.id) for ch in chunks]
    asset_ids = [f"region_{reg.id}" for reg in regions if (reg.region_type or "") != "text"]

    deleted_text_embeddings, deleted_asset_embeddings = _delete_chroma_ids(
        text_ids=text_ids,
        asset_ids=asset_ids,
    )

    deleted_files = 0
    raw_paths = [doc.stored_path, *[p.image_path for p in pages], *[r.image_path for r in regions]]
    seen_paths: set[str] = set()
    for path_value in raw_paths:
        if path_value and path_value not in seen_paths:
            seen_paths.add(path_value)
            if _safe_remove_path(path_value):
                deleted_files += 1

    page_dir = settings.PAGES_DIR / org_scope(organization_id) / str(doc.project_id) / str(doc.id)
    if page_dir.exists() and _safe_remove_path(str(page_dir)):
        deleted_files += 1

    upload_parent = None
    if doc.stored_path:
        upload_parent = str(Path(doc.stored_path).parent)

    db.delete(doc)
    db.commit()

    _cleanup_empty_parents(upload_parent, settings.UPLOADS_DIR)
    _cleanup_empty_parents(str(page_dir), settings.PAGES_DIR)

    return DeleteDocumentResponse(
        document_id=document_id,
        deleted_text_embeddings=deleted_text_embeddings,
        deleted_asset_embeddings=deleted_asset_embeddings,
        deleted_files=deleted_files,
    )


@app.get("/layout/pages", response_model=list[PageItem])
def list_pages(document_id: int, db: Session = Depends(get_db)):
    return db.execute(select(Page).where(Page.document_id == document_id).order_by(Page.page_number.asc())).scalars().all()


@app.get("/layout/regions", response_model=list[RegionItem])
def list_regions(page_id: int, db: Session = Depends(get_db)):
    rows = db.execute(select(Region).where(Region.page_id == page_id).order_by(Region.id.asc())).scalars().all()
    return [
        RegionItem(
            id=r.id,
            page_id=r.page_id,
            region_type=r.region_type,
            bbox=r.bbox,
            summary=r.summary,
            ocr_text=r.ocr_text,
            image_path=r.image_path,
        )
        for r in rows
    ]


@app.get("/layout/source_fragments", response_model=list[SourceFragmentExportItem])
def export_source_fragments(document_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    document = db.execute(
        select(Document).where(Document.id == int(document_id), Document.organization_id == organization_id)
    ).scalar_one_or_none()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    pages = db.execute(
        select(Page).where(Page.document_id == int(document_id)).order_by(Page.page_number.asc(), Page.id.asc())
    ).scalars().all()
    page_by_id = {int(page.id): page for page in pages}
    page_by_number = {int(page.page_number or 0): page for page in pages}

    fragments: list[SourceFragmentExportItem] = []
    for page in pages:
        if str(page.text_raw or "").strip():
            fragments.append(
                SourceFragmentExportItem(
                    external_id=f"page:{page.id}",
                    fragment_type="page",
                    page=int(page.page_number or 0),
                    bbox=[],
                    text=_clip(str(page.text_raw or ""), 6000),
                    extractor="rag_page_text",
                    confidence=0.85,
                    metadata={
                        "rag_document_id": int(document.id),
                        "rag_page_id": int(page.id),
                        "filename": str(document.filename or ""),
                        "width": int(page.width or 0),
                        "height": int(page.height or 0),
                    },
                )
            )

    regions = db.execute(
        select(Region).where(Region.project_id == int(document.project_id), Region.organization_id == organization_id).order_by(Region.id.asc())
    ).scalars().all()
    for region in regions:
        page = page_by_id.get(int(region.page_id))
        if not page or int(page.document_id) != int(document.id):
            continue
        text_parts = [str(region.summary or "").strip(), str(region.ocr_text or "").strip()]
        text_value = "\n".join(part for part in text_parts if part)
        if not text_value:
            continue
        fragments.append(
            SourceFragmentExportItem(
                external_id=f"region:{region.id}",
                fragment_type=str(region.region_type or "region"),
                page=int(page.page_number or 0),
                bbox=region.bbox,
                text=_clip(text_value, 6000),
                extractor="rag_layout_region",
                confidence=0.8,
                metadata={
                    "rag_document_id": int(document.id),
                    "rag_page_id": int(page.id),
                    "rag_region_id": int(region.id),
                    "filename": str(document.filename or ""),
                    "has_image": bool(region.image_path),
                    "width": int(page.width or 0),
                    "height": int(page.height or 0),
                },
            )
        )

    chunks = db.execute(
        select(Chunk).where(Chunk.document_id == int(document.id)).order_by(Chunk.page_number.asc(), Chunk.id.asc())
    ).scalars().all()
    region_by_id = {int(region.id): region for region in regions}
    for chunk in chunks:
        text_value = str(chunk.text or "").strip()
        if not text_value:
            continue
        page = page_by_number.get(int(chunk.page_number or 0))
        region = region_by_id.get(int(chunk.region_id)) if chunk.region_id is not None else None
        fragments.append(
            SourceFragmentExportItem(
                external_id=f"chunk:{chunk.id}",
                fragment_type="text",
                page=int(chunk.page_number or 0),
                bbox=region.bbox if region else [],
                text=_clip(text_value, 6000),
                extractor="rag_chunk",
                confidence=0.75,
                metadata={
                    "rag_document_id": int(document.id),
                    "rag_page_id": int(page.id) if page else None,
                    "rag_chunk_id": int(chunk.id),
                    "rag_region_id": int(chunk.region_id) if chunk.region_id is not None else None,
                    "embedding_id": str(chunk.embedding_id or ""),
                    "filename": str(document.filename or ""),
                    "width": int(page.width or 0) if page else 0,
                    "height": int(page.height or 0) if page else 0,
                },
            )
        )

    return fragments


@app.get("/layout/page_image")
def page_image(page_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    p = db.get(Page, page_id)
    if not p or not p.image_path or p.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Page image not found")
    return FileResponse(p.image_path)


@app.get("/layout/page_download")
def page_download(page_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    p = db.get(Page, page_id)
    if not p or p.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Page not found")
    document = db.get(Document, p.document_id)
    stem = Path(str(document.filename or f'page_{page_id}')).stem if document else f'page_{page_id}'
    is_docx = str(getattr(document, 'filename', '') or '').lower().endswith('.docx')
    if is_docx and p.text_raw:
        pdf_bytes = _build_text_pdf_bytes([(p, document)])
        filename = f"{stem}_sheet_{int(p.page_number or 0)}.pdf"
        headers = _build_download_headers(filename)
        return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
    if p.image_path and Path(p.image_path).exists():
        filename = f"{stem}_sheet_{int(p.page_number or 0)}.png"
        return FileResponse(p.image_path, media_type="image/png", filename=filename)
    if p.text_raw:
        pdf_bytes = _build_text_pdf_bytes([(p, document)])
        filename = f"{stem}_sheet_{int(p.page_number or 0)}.pdf"
        headers = _build_download_headers(filename)
        return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
    raise HTTPException(status_code=404, detail="Page content not found")


@app.get("/layout/pages_pdf")
def pages_pdf(page_ids: str, organization_id: int | None = None, db: Session = Depends(get_db)):
    raw = [part.strip() for part in str(page_ids or "").split(",") if part.strip()]
    parsed: list[int] = []
    for part in raw:
        try:
            value = int(part)
        except Exception:
            continue
        if value > 0 and value not in parsed:
            parsed.append(value)
    if not parsed:
        raise HTTPException(status_code=400, detail="No page ids provided")

    page_pairs: list[tuple[Page, Document | None]] = []
    for page_id in parsed:
        page = db.get(Page, page_id)
        if not page or page.organization_id != organization_id:
            continue
        document = db.get(Document, page.document_id)
        page_pairs.append((page, document))
    if not page_pairs:
        raise HTTPException(status_code=404, detail="Pages not found")

    pdf_bytes = _build_text_pdf_bytes(page_pairs)
    doc_ids = {int(p.document_id) for p, _ in page_pairs}
    if len(doc_ids) == 1:
        document = page_pairs[0][1]
        stem = Path(str(document.filename or "sheets")).stem if document else "sheets"
        filename = f"{stem}_selected_pages.pdf"
    else:
        filename = "selected_pages.pdf"

    headers = _build_download_headers(filename)
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/layout/region_image")
def region_image(region_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    r = db.get(Region, region_id)
    if not r or not r.image_path or r.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Region image not found")
    return FileResponse(r.image_path)


def _parse_bbox(raw: object) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, list):
        out: list[int] = []
        for item in raw:
            try:
                out.append(int(item))
            except Exception:
                continue
        return out
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except Exception:
            try:
                value = ast.literal_eval(text)
            except Exception:
                return []
        return _parse_bbox(value)
    return []




def _build_download_headers(filename: str) -> dict[str, str]:
    safe = str(filename or 'download.pdf').replace('\r', ' ').replace('\n', ' ').strip() or 'download.pdf'
    ascii_name = re.sub(r'[^A-Za-z0-9._-]+', '_', safe).strip('._') or 'download'
    if '.' not in ascii_name and '.' in safe:
        suffix = Path(safe).suffix
        if suffix and not ascii_name.endswith(suffix):
            ascii_name = f"{ascii_name}{suffix}"
    encoded = quote(safe, safe='')
    return {
        'Content-Disposition': f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"
    }


def _find_text_fontfile() -> str | None:
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
    ]
    for item in candidates:
        if Path(item).exists():
            return item
    return None


def _build_text_pdf_bytes(page_pairs: list[tuple[Page, Document | None]]) -> bytes:
    if fitz is None:
        images: list[Image.Image] = []
        for page, _document in page_pairs:
            if page.image_path and Path(page.image_path).exists():
                try:
                    images.append(Image.open(page.image_path).convert('RGB'))
                    continue
                except Exception:
                    pass
            img = Image.new('RGB', (1200, 1700), 'white')
            images.append(img)
        buffer = BytesIO()
        first, rest = images[0], images[1:]
        first.save(buffer, format='PDF', save_all=True, append_images=rest)
        for image in images:
            try:
                image.close()
            except Exception:
                pass
        return buffer.getvalue()

    out = fitz.open()
    fontfile = _find_text_fontfile()
    for page, document in page_pairs:
        use_text_page = str(getattr(document, 'filename', '') or '').lower().endswith('.docx') or not (page.image_path and Path(page.image_path).exists())
        if use_text_page and (page.text_raw or '').strip():
            pdf_page = out.new_page(width=595, height=842)
            rect = fitz.Rect(40, 46, 555, 796)
            title = f"{str(getattr(document, 'filename', '') or 'Документ')} — лист {int(page.page_number or 0)}"
            pdf_page.insert_textbox(fitz.Rect(40, 24, 555, 48), title, fontsize=12, fontfile=fontfile, fontname='F0' if fontfile else 'helv', color=(0, 0, 0))
            text_value = str(page.text_raw or '').strip()
            if not text_value:
                text_value = 'Текст страницы недоступен.'
            pdf_page.insert_textbox(rect, text_value, fontsize=10.5, fontfile=fontfile, fontname='F0' if fontfile else 'helv', color=(0, 0, 0), align=0)
            continue

        if page.image_path and Path(page.image_path).exists():
            img = Image.open(page.image_path)
            width, height = img.size
            img.close()
            pdf_page = out.new_page(width=float(width), height=float(height))
            pdf_page.insert_image(fitz.Rect(0, 0, float(width), float(height)), filename=page.image_path)
            continue

        pdf_page = out.new_page(width=595, height=842)
        pdf_page.insert_textbox(fitz.Rect(40, 60, 555, 780), str(page.text_raw or 'Страница без превью'), fontsize=11, fontfile=fontfile, fontname='F0' if fontfile else 'helv', color=(0, 0, 0))
    pdf_bytes = out.tobytes()
    out.close()
    return pdf_bytes

def _asset_label(region_type: str) -> str:
    return REGION_TYPE_LABELS.get(region_type, region_type or "Материал")


def _clip(text: str | None, limit: int = 1200) -> str:
    value = " ".join((text or "").replace("\r", "\n").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _rank_key(score: float | None, page_number: int, region_type: str) -> tuple[float, int, int]:
    return (
        float(score) if score is not None else 999.0,
        REGION_PRIORITY.get(region_type, 50),
        page_number,
    )


def _region_to_asset(*, region: Region, page: Page, document: Document, score: float | None = None, selected: bool = False, linked_chunk_ids: list[int] | None = None) -> SourceAsset:
    return SourceAsset(
        region_id=int(region.id),
        page_id=int(page.id),
        document_id=int(document.id),
        filename=str(document.filename or ""),
        page_number=int(page.page_number or 0),
        region_type=str(region.region_type or ""),
        summary=str(region.summary or _asset_label(str(region.region_type or ""))) or None,
        ocr_text=_clip(region.ocr_text, 1000) or None,
        bbox=region.bbox,
        score=score,
        selected=selected,
        has_image=bool(region.image_path),
        linked_chunk_ids=[int(x) for x in (linked_chunk_ids or [])],
    )


def _build_viewer_pages(selected_sources: list[SourceChunk], selected_assets: list[SourceAsset], all_sources: list[SourceChunk], all_assets: list[SourceAsset]) -> list[PageViewerItem]:
    out: list[PageViewerItem] = []
    seen: set[int] = set()

    def add(page_id: int | None, document_id: int, filename: str, page_number: int):
        if page_id is None:
            return
        try:
            pid = int(page_id)
        except Exception:
            return
        if pid <= 0 or pid in seen:
            return
        seen.add(pid)
        out.append(PageViewerItem(page_id=pid, document_id=int(document_id), filename=str(filename or ""), page_number=int(page_number or 0)))

    for src in selected_sources:
        add(src.page_id, src.document_id, src.filename, src.page_number)
    for asset in selected_assets:
        add(asset.page_id, asset.document_id, asset.filename, asset.page_number)
    if not out:
        for src in all_sources[:3]:
            add(src.page_id, src.document_id, src.filename, src.page_number)
    if not out:
        for asset in all_assets[:3]:
            add(asset.page_id, asset.document_id, asset.filename, asset.page_number)
    return out[:8]


def _fallback_answer_from_chunks(question: str, chunks: list[dict], error: Exception | None = None) -> tuple[str, list[int]]:
    fallback_chunk_ids = [int(c["chunk_id"]) for c in chunks[: min(4, len(chunks))] if c.get("chunk_id") is not None]
    first = chunks[0] if chunks else {}
    excerpt = _clip(str(first.get("text") or ""), 900)
    ref_lines: list[str] = []
    seen: set[tuple[str, int]] = set()
    for chunk in chunks[:6]:
        key = (str(chunk.get("filename") or "Документ"), int(chunk.get("page_number") or 0))
        if key in seen:
            continue
        seen.add(key)
        ref_lines.append(f"- {key[0]}, лист {key[1]}")

    lines = [
        "LLM-сервис сейчас недоступен, поэтому показываю найденный контекст без финальной генерации ответа.",
    ]
    if excerpt:
        lines.append(f"\nНаиболее близкий найденный фрагмент:\n{excerpt}")
    if ref_lines:
        lines.append("\nГде смотреть в документации:\n" + "\n".join(ref_lines))
    lines.append(f"\nВопрос: {question}")
    if error:
        lines.append(f"\nТехническая причина: {error.__class__.__name__}: {_clip(str(error), 240)}")
    return "\n".join(lines).strip(), fallback_chunk_ids


def _select_related_assets(db: Session, sources: list[SourceChunk], project_id: int, organization_id: int | None, question: str) -> list[SourceAsset]:
    seen: dict[int, SourceAsset] = {}
    document_cache: dict[int, Document | None] = {}
    page_cache: dict[tuple[int, int], Page | None] = {}

    def get_doc(doc_id: int) -> Document | None:
        if doc_id not in document_cache:
            document_cache[doc_id] = db.get(Document, doc_id)
        return document_cache[doc_id]

    def add_asset(region: Region, page: Page, document: Document, score: float | None, linked_chunk_id: int | None):
        existing = seen.get(region.id)
        linked = [linked_chunk_id] if linked_chunk_id is not None else []
        if existing is None:
            seen[region.id] = _region_to_asset(region=region, page=page, document=document, score=score, linked_chunk_ids=linked)
            return
        if linked_chunk_id is not None and linked_chunk_id not in existing.linked_chunk_ids:
            existing.linked_chunk_ids.append(linked_chunk_id)
        if existing.score is None and score is not None:
            existing.score = score
        elif score is not None:
            existing.score = min(existing.score or score, score)

    # Semantic asset search
    if bool(getattr(settings, "ENABLE_ASSET_SEARCH", False)):
        where = {"project_id": project_id, "organization_id": int(organization_id or 0)}
        for hit in get_vectorstores().query_asset(query_text=question, top_k=max(8, min(18, len(sources) * 2 or 8)), where=where):
            meta = hit.get("meta") or {}
            try:
                region_id = int(meta.get("region_id"))
            except Exception:
                continue
            region = db.get(Region, region_id)
            if not region or region.organization_id != organization_id:
                continue
            page = db.get(Page, region.page_id)
            document = get_doc(page.document_id) if page else None
            if not page or not document:
                continue
            add_asset(region, page, document, float(hit.get("distance")) if hit.get("distance") is not None else None, None)

    # Nearby assets from the same pages as retrieved chunks
    for src in sources:
        key = (src.document_id, src.page_number)
        if key not in page_cache:
            page_cache[key] = db.execute(
                select(Page).where(
                    Page.document_id == src.document_id,
                    Page.page_number == src.page_number,
                    Page.organization_id == organization_id,
                )
            ).scalar_one_or_none()
        page = page_cache.get(key)
        document = get_doc(src.document_id)
        if not page or not document:
            continue
        regs = db.execute(
            select(Region).where(Region.page_id == page.id, Region.region_type != "text").order_by(Region.id.asc())
        ).scalars().all()
        regs.sort(key=lambda r: (REGION_PRIORITY.get(str(r.region_type or ""), 50), r.id))
        for region in regs[:4]:
            add_asset(region, page, document, src.score, src.chunk_id)

    assets = list(seen.values())
    assets.sort(key=lambda a: _rank_key(a.score, a.page_number, a.region_type))
    return assets[:12]


@app.post("/assets/search", response_model=AssetSearchResponse)
def search_assets(payload: AssetSearchRequest):
    if not bool(getattr(settings, "ENABLE_ASSET_SEARCH", False)):
        return AssetSearchResponse(hits=[])
    where = {"project_id": payload.project_id, "organization_id": int(payload.organization_id or 0)}
    hits: list[AssetSearchHit] = []
    for r in get_vectorstores().query_asset(query_text=payload.query, top_k=payload.top_k, where=where):
        meta = r.get("meta") or {}
        rt = str(meta.get("region_type") or "")
        if payload.region_types and rt not in payload.region_types:
            continue
        hits.append(
            AssetSearchHit(
                region_id=int(meta.get("region_id")),
                document_id=int(meta.get("document_id")),
                filename=str(meta.get("filename") or ""),
                page_number=int(meta.get("page_number") or 0),
                region_type=rt,
                bbox=_parse_bbox(meta.get("bbox")),
                summary=None,
                score=float(r.get("distance")) if r.get("distance") is not None else float(r.get("_rank")) if r.get("_rank") is not None else None,
            )
        )
    return AssetSearchResponse(hits=hits)


@app.post("/ask", response_model=AskResponse)
def ask(payload: AskRequest, db: Session = Depends(get_db)):
    retrieved = _hybrid_query_text(
        db,
        question=payload.question,
        project_id=payload.project_id,
        organization_id=payload.organization_id,
        top_k=payload.top_k,
    )
    if not retrieved:
        raise HTTPException(status_code=404, detail="No relevant context found in project documents")

    sources: list[SourceChunk] = []
    structured_chunks: list[dict] = []
    pairs = {(int((r.get('meta') or {}).get('document_id') or 0), int((r.get('meta') or {}).get('page_number') or 0)) for r in retrieved}
    page_id_by_doc_page: dict[tuple[int, int], int] = {}
    if pairs:
        docs = sorted({doc_id for doc_id, _ in pairs if doc_id > 0})
        rows = db.execute(select(Page).where(Page.document_id.in_(docs), Page.organization_id == payload.organization_id)).scalars().all()
        for page in rows:
            page_id_by_doc_page[(int(page.document_id), int(page.page_number))] = int(page.id)

    for r in retrieved:
        meta = r.get("meta") or {}
        chunk_id = int(meta.get("chunk_id")) if meta.get("chunk_id") is not None else None
        text_value = str(r.get("text") or "").strip()
        if not text_value:
            continue
        page_id = meta.get("page_id")
        if page_id is None:
            page_id = page_id_by_doc_page.get((int(meta.get("document_id") or 0), int(meta.get("page_number") or 0)))
        score = float(r.get("_rank")) if r.get("_rank") is not None else float(r.get("distance")) if r.get("distance") is not None else None
        structured_chunks.append(
            {
                "chunk_id": chunk_id,
                "document_id": int(meta.get("document_id") or 0),
                "filename": str(meta.get("filename") or ""),
                "page_id": int(page_id) if page_id is not None else None,
                "page_number": int(meta.get("page_number") or 0),
                "text": text_value,
                "score": score,
            }
        )
        sources.append(
            SourceChunk(
                chunk_id=chunk_id,
                page_id=int(page_id) if page_id is not None else None,
                document_id=int(meta.get("document_id") or 0),
                filename=str(meta.get("filename") or ""),
                page_number=int(meta.get("page_number") or 0),
                text=_clip(text_value, 1800),
                region_id=None,
                score=score,
                selected=False,
                related_asset_ids=[],
            )
        )

    if not structured_chunks:
        raise HTTPException(status_code=404, detail="No relevant text chunks found in project documents")

    try:
        answer, selected_chunk_ids, _selected_asset_ids, _raw = ask_structured_answer(
            question=payload.question,
            chunks=structured_chunks,
            assets=[],
            system_prompt=settings.SYSTEM_PROMPT,
            model=settings.QWEN_MODEL,
        )
    except Exception as e:
        logger.warning("llm_structured_answer_failed project_id=%s: %s", payload.project_id, e)
        answer, selected_chunk_ids = _fallback_answer_from_chunks(payload.question, structured_chunks, e)

    if not selected_chunk_ids:
        selected_chunk_ids = [src.chunk_id for src in sources[: min(4, len(sources))] if src.chunk_id is not None]
    selected_chunk_ids = [int(x) for x in selected_chunk_ids if x is not None]
    selected_chunk_set = set(selected_chunk_ids)
    for src in sources:
        if src.chunk_id is not None and src.chunk_id in selected_chunk_set:
            src.selected = True

    sources.sort(key=lambda s: (not s.selected, s.page_number, s.filename, -(len(s.text or ''))))
    visible_sources = select_visible_sources(sources)
    visible_selected_sources = [s for s in visible_sources if s.selected]
    pages = _build_viewer_pages(visible_selected_sources, [], sources, [])
    sources = visible_sources
    return AskResponse(
        answer=answer,
        selected_chunk_ids=selected_chunk_ids,
        selected_asset_ids=[],
        pages=pages,
        sources=sources,
        assets=[],
    )
