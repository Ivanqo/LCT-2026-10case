from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..db.models import DocumentVersion, UploadJob


def calculate_content_hash(path: Path | None) -> str | None:
    if not path or not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_document_stage(filename: str | None, source_type: str | None = None) -> str:
    name = (filename or "").lower()
    if source_type == "ifc" or ".ifc" in name:
        return "ifc"
    if any(token in name for token in ["исполнитель", "as-built", "asbuilt", "_и_", "-и-", " стадия и"]):
        return "as_built"
    if any(token in name for token in ["рабоч", "working", "_р_", "-р-", "рд"]):
        return "working"
    if any(token in name for token in ["проект", "project", "_п_", "-п-", "пд"]):
        return "project"
    return "unknown"


def infer_document_code(filename: str | None) -> str | None:
    if not filename:
        return None
    stem = Path(filename).stem
    cleaned = re.sub(r"\s+", "-", stem.strip())
    cleaned = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_.-]+", "-", cleaned).strip("-")
    return cleaned[:255] or None


def infer_revision(filename: str | None) -> str | None:
    if not filename:
        return None
    match = re.search(r"(?:rev|revision|ред|изм)[\s._-]*([a-zа-я0-9]+)", filename, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def infer_approval_status(filename: str | None) -> str:
    name = (filename or "").lower()
    if any(token in name for token in ["утвержд", "approved", "signed", "ifc", "issued-for-construction"]):
        return "APPROVED"
    if any(token in name for token in ["чернов", "draft", "неутвержд"]):
        return "DRAFT"
    return "UNKNOWN"


def infer_approval_date(filename: str | None) -> datetime | None:
    if not filename:
        return None
    match = re.search(r"(20\d{2})[-_.](0?[1-9]|1[0-2])[-_.](0?[1-9]|[12]\d|3[01])", filename)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def find_current_version_by_hash(
    db: Session,
    *,
    project_id: int,
    organization_id: int,
    document_stage: str | None,
    document_code: str | None,
    content_hash: str | None,
) -> DocumentVersion | None:
    """The current (chain-tip) DocumentVersion for this document family that
    already carries this exact content, if any. Used to recognize a
    duplicate upload -- same bytes re-submitted for the same document -- so
    it can be treated as a no-op instead of triggering a fresh recompute."""
    if not content_hash or not document_code:
        return None
    return (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.project_id == int(project_id),
            DocumentVersion.organization_id == int(organization_id),
            DocumentVersion.document_stage == (document_stage or "unknown"),
            DocumentVersion.document_code == document_code,
            DocumentVersion.content_hash == content_hash,
            DocumentVersion.successor_id.is_(None),
        )
        .order_by(DocumentVersion.id.desc())
        .first()
    )


def ensure_document_version_for_upload_job(db: Session, job: UploadJob, file_path: Path | None = None) -> tuple[DocumentVersion, bool]:
    """Returns (document_version, changed). `changed` is False when this
    upload's content is byte-identical to the current version already on
    file for this document family (same project/stage/document_code/hash):
    the caller must not trigger a recompute for it -- a duplicate
    upload/hash must not do the work again."""
    source_type = str(job.source_type or "rag")
    source_document_id = int(job.document_id) if job.document_id is not None else None
    existing = (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.project_id == int(job.project_id),
            DocumentVersion.organization_id == int(job.organization_id or 0),
            DocumentVersion.source_type == source_type,
            DocumentVersion.source_document_id == source_document_id,
            DocumentVersion.filename == str(job.filename),
        )
        .first()
    )
    content_hash = calculate_content_hash(file_path)
    if existing:
        changed = bool(content_hash) and existing.content_hash != content_hash
        if not existing.file_hash and existing.content_hash:
            existing.file_hash = existing.content_hash
        if not existing.file_path and file_path:
            existing.file_path = str(file_path)
        if not existing.doc_stage:
            existing.doc_stage = existing.document_stage
        db.add(existing)
        db.flush()
        return existing, changed

    document_stage = infer_document_stage(job.filename, source_type)
    document_code = infer_document_code(job.filename)

    duplicate = find_current_version_by_hash(
        db,
        project_id=int(job.project_id),
        organization_id=int(job.organization_id or 0),
        document_stage=document_stage,
        document_code=document_code,
        content_hash=content_hash,
    )
    if duplicate:
        return duplicate, False

    doc_version = DocumentVersion(
        project_id=int(job.project_id),
        organization_id=int(job.organization_id or 0),
        object_id=f"OBJ-{int(job.project_id):06d}",
        source_type=source_type,
        source_document_id=source_document_id,
        filename=str(job.filename),
        doc_stage=document_stage,
        document_stage=document_stage,
        discipline=None,
        document_code=document_code,
        version="v1",
        revision=infer_revision(job.filename),
        approval_status=infer_approval_status(job.filename),
        approval_date=infer_approval_date(job.filename),
        content_hash=content_hash,
        file_hash=content_hash,
        file_path=str(file_path) if file_path else None,
    )
    db.add(doc_version)
    db.flush()
    _link_revision_chain(db, doc_version)
    return doc_version, True


def _link_revision_chain(db: Session, doc_version: DocumentVersion) -> None:
    previous = (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.id != int(doc_version.id),
            DocumentVersion.project_id == int(doc_version.project_id),
            DocumentVersion.organization_id == int(doc_version.organization_id),
            DocumentVersion.document_stage == doc_version.document_stage,
            DocumentVersion.document_code == doc_version.document_code,
            DocumentVersion.successor_id.is_(None),
        )
        .order_by(DocumentVersion.uploaded_at.desc(), DocumentVersion.id.desc())
        .first()
    )
    if not previous:
        return
    doc_version.predecessor_id = int(previous.id)
    previous.successor_id = int(doc_version.id)
    db.add(previous)
    db.add(doc_version)
    db.flush()
