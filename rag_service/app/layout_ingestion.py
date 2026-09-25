from __future__ import annotations

from pathlib import Path
from typing import Tuple

from sqlalchemy.orm import Session

from .ingestion import ingest_document_text
from .vectorstore import VectorStores


def ingest_pdf_layout(
    *,
    db: Session,
    vectorstores: VectorStores,
    project_id: int,
    organization_id: int | None,
    stored_path: Path,
    original_filename: str,
    **_: object,
) -> Tuple[int, int, int, int, int]:
    """Text-first PDF ingestion.

    Previous versions tried to split pages into many visual entities (tables, drawings,
    title blocks). The new pipeline intentionally indexes only page-bound text blocks.
    We still keep page previews for the frontend viewer, but regions/assets are omitted.
    """

    doc_id, pages_created, text_chunks_created = ingest_document_text(
        db=db,
        vectorstores=vectorstores,
        project_id=project_id,
        organization_id=organization_id,
        file_type="pdf",
        stored_path=stored_path,
        original_filename=original_filename,
    )
    return doc_id, pages_created, 0, text_chunks_created, 0
