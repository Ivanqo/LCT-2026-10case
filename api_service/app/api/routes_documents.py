from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from ..clients.ifc_client import IfcClient
from ..clients.rag_client import RagClient
from ..db.models import UploadJob, User
from ..db.session import get_db
from ..schemas import DeleteDocumentOut, DocumentItem, ReindexDocumentOut
from ..services.simple_documents import document_to_item, list_project_documents
from .auth import get_current_user
from .utils import get_project_for_org

router = APIRouter(tags=["documents"])


def _require_org(user: User) -> int:
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")
    return int(user.organization_id)


def _ifc_model_to_document_item(row: dict) -> DocumentItem:
    return DocumentItem(
        id=int(row["model_id"]),
        project_id=int(row["project_id"]),
        filename=str(row.get("original_filename") or "IFC model"),
        uploaded_at=row["created_at"],
        source_type="ifc",
        status=str(row.get("status") or "queued"),
        stage="IFC indexed" if row.get("status") == "indexed" else "IFC indexing",
        progress=100 if row.get("status") == "indexed" else 50,
        detail=row.get("error"),
        processing_status=str(row.get("status") or "queued"),
        processing_progress=100 if row.get("status") == "indexed" else 50,
        is_ready=row.get("status") == "indexed",
        error_message=row.get("error"),
    )


def _upload_job_to_document_item(job: UploadJob) -> DocumentItem:
    status = str(job.status or "queued")
    return DocumentItem(
        id=int(job.id),
        project_id=int(job.project_id),
        filename=str(job.filename),
        uploaded_at=job.created_at,
        source_type="upload_job",
        status=status,
        stage=job.stage,
        progress=int(job.progress or 0),
        detail=job.detail,
        processing_status=status,
        processing_progress=int(job.progress or 0),
        is_ready=False,
        error_message=job.detail if status == "failed" else None,
    )


@router.get("/documents", response_model=list[DocumentItem])
async def list_documents(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)

    items: list[DocumentItem] = []

    upload_jobs = (
        db.query(UploadJob)
        .filter(
            UploadJob.project_id == int(project_id),
            UploadJob.organization_id == organization_id,
            UploadJob.status.in_(["queued", "processing", "failed"]),
        )
        .order_by(UploadJob.created_at.desc())
        .all()
    )
    items.extend(_upload_job_to_document_item(job) for job in upload_jobs)

    # Keep locally stored files visible for backwards compatibility.
    docs = list_project_documents(
        db,
        organization_id=organization_id,
        project_id=int(project_id),
    )
    items.extend(DocumentItem(**document_to_item(doc)) for doc in docs)

    try:
        rag_docs = await RagClient(timeout=3.0, retries=1).list_documents(
            project_id=int(project_id),
            organization_id=organization_id,
        )
        items.extend(DocumentItem(**row) for row in rag_docs)
    except Exception:
        # RAG may be temporarily unavailable during startup; do not hide IFC/simple docs.
        pass

    try:
        ifc_models = await IfcClient().list_models(
            project_id=int(project_id),
            organization_id=organization_id,
        )
        items.extend(_ifc_model_to_document_item(row) for row in ifc_models)
    except Exception:
        pass

    return sorted(items, key=lambda item: item.uploaded_at, reverse=True)


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: int,
    project_id: int = Query(...),
    source_type: str = Query("rag", pattern="^(simple|upload_job|rag|ifc)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)

    if source_type == "ifc":
        raise HTTPException(status_code=400, detail="IFC download is not available via API proxy")

    if source_type == "upload_job":
        raise HTTPException(status_code=400, detail="Upload job original download is not available")

    if source_type == "simple":
        from pathlib import Path

        from ..services.simple_documents import get_document_for_org

        doc = get_document_for_org(
            db,
            document_id=document_id,
            organization_id=organization_id,
            project_id=project_id,
        )
        path = Path(doc.storage_path)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="Original file not found")
        if path.suffix.lower() == ".pdf":
            media_type = "application/pdf"
        else:
            media_type = "application/octet-stream"
        response = Response(content=path.read_bytes(), media_type=media_type)
        response.headers["Content-Disposition"] = f'attachment; filename="{doc.filename}"'
        return response

    data, media_type, headers = await RagClient().download_document(
        document_id=document_id,
        organization_id=organization_id,
    )
    response = Response(content=data, media_type=media_type)
    if headers.get("content-disposition"):
        response.headers["Content-Disposition"] = headers["content-disposition"]
    return response


@router.post("/documents/{document_id}/reindex", response_model=ReindexDocumentOut)
async def reindex_document(
    document_id: int,
    project_id: int = Query(...),
    source_type: str = Query("rag", pattern="^(simple|upload_job|rag|ifc)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)

    if source_type != "rag":
        raise HTTPException(status_code=400, detail="Reindex is available only for RAG PDF/DOCX documents")

    result = await RagClient(timeout=900.0, retries=1).reindex_document(
        document_id=document_id,
        project_id=project_id,
        organization_id=organization_id,
    )
    return ReindexDocumentOut(source_type="rag", **result)


@router.delete("/documents/{document_id}", response_model=DeleteDocumentOut)
async def delete_document(
    document_id: int,
    project_id: int = Query(...),
    source_type: str = Query("rag", pattern="^(simple|upload_job|rag|ifc)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)

    if source_type == "ifc":
        result = await IfcClient().delete_model(
            model_id=document_id,
            organization_id=organization_id,
        )
        return DeleteDocumentOut(
            source_type="ifc",
            document_id=int(result["model_id"]),
            deleted_files=int(result.get("deleted_files") or 0),
        )

    if source_type == "upload_job":
        from pathlib import Path

        job = (
            db.query(UploadJob)
            .filter(
                UploadJob.id == int(document_id),
                UploadJob.organization_id == organization_id,
                UploadJob.project_id == int(project_id),
            )
            .first()
        )
        if not job:
            raise HTTPException(status_code=404, detail="Upload job not found")
        deleted_files = 0
        if job.temp_path and Path(job.temp_path).exists():
            Path(job.temp_path).unlink()
            deleted_files = 1
        db.delete(job)
        db.commit()
        return DeleteDocumentOut(
            source_type="upload_job",
            document_id=document_id,
            deleted_files=deleted_files,
        )

    if source_type == "simple":
        from pathlib import Path

        from ..services.simple_documents import get_document_for_org

        doc = get_document_for_org(
            db,
            document_id=document_id,
            organization_id=organization_id,
            project_id=project_id,
        )
        deleted_files = 0
        for raw in (doc.storage_path, doc.meta_path):
            if raw and Path(raw).exists():
                Path(raw).unlink()
                deleted_files += 1
        db.delete(doc)
        db.commit()
        return DeleteDocumentOut(
            source_type="simple",
            document_id=document_id,
            deleted_files=deleted_files,
        )

    result = await RagClient().delete_document(
        document_id=document_id,
        organization_id=organization_id,
    )
    return DeleteDocumentOut(source_type="rag", **result)
