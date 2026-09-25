from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from .config import settings
from .db.models import Chunk, Document, Page, Region

logger = logging.getLogger(__name__)


def _safe_rmtree(p: Path) -> None:
    try:
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        logger.exception("Failed to remove dir: %s", p)


def _safe_unlink(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except Exception:
        logger.exception("Failed to remove file: %s", p)


def delete_document_all(*, db: Session, organization_id: int | None, project_id: int, document_id: int) -> None:
    """Delete a document and all derived artifacts.

    Removes:
      - DB rows: regions, pages, chunks, document
      - Files: uploads/pdf, rendered pages, region crops
      - Chroma records (best-effort) by metadata filter (project_id + document_id)
    """

    doc = (
        db.query(Document)
        .filter(Document.project_id == int(project_id), Document.id == int(document_id), Document.organization_id == organization_id)
        .first()
    )
    if not doc:
        raise ValueError(f"Document not found: project_id={project_id} document_id={document_id}")

    # Collect paths before deleting DB rows
    stored_path = Path(doc.stored_path) if doc.stored_path else None
    pages_dir = settings.PAGES_DIR / f"org_{organization_id or 0}" / str(project_id) / str(document_id)
    regions_dir = settings.REGIONS_DIR / f"org_{organization_id or 0}" / str(project_id) / str(document_id)

    # DB delete (children first)
    # Regions -> Pages -> Chunks -> Document
    pages = db.query(Page).filter(Page.document_id == int(document_id)).all()
    page_ids = [p.id for p in pages]
    if page_ids:
        db.query(Region).filter(Region.page_id.in_(page_ids)).delete(synchronize_session=False)
    db.query(Page).filter(Page.document_id == int(document_id)).delete(synchronize_session=False)
    db.query(Chunk).filter(Chunk.document_id == int(document_id)).delete(synchronize_session=False)
    db.query(Document).filter(Document.id == int(document_id)).delete(synchronize_session=False)
    db.commit()

    # Files cleanup
    _safe_rmtree(pages_dir)
    _safe_rmtree(regions_dir)
    if stored_path:
        _safe_unlink(stored_path)

    # Chroma cleanup (best-effort)
    try:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        client = chromadb.PersistentClient(
            path=str(settings.VECTORSTORE_DIR),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        col_text = client.get_or_create_collection(name=settings.COLLECTION_TEXT)
        col_asset = client.get_or_create_collection(name=settings.COLLECTION_ASSET)
        where = {"organization_id": int(organization_id or 0), "project_id": int(project_id), "document_id": int(document_id)}
        col_text.delete(where=where)
        col_asset.delete(where=where)
    except Exception:
        # If Chroma isn't installed or persistence isn't present, ignore.
        logger.info("Chroma cleanup skipped/failed (best-effort)")
