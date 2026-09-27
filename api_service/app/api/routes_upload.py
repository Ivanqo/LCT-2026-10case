from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from pathlib import PurePath

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import batch_package as bp
from ..config import settings
from ..clients.ifc_client import IfcClient
from ..clients.rag_client import RagClient
from ..db.models import AuditLog, DocumentVersion, InspectionProcess, SourceFragment, UploadJob, User
from ..db.session import SessionLocal, get_db
from ..domain.document_versions import ensure_document_version_for_upload_job
from ..domain.v3_jobs import enqueue_process_job, execute_process_job, publish_job_message
from ..domain.v3_pipeline import get_or_create_open_process, impact_scope_for_documents
from ..request_context import get_client_ip
from ..schemas import UploadResponse
from ..services.simple_documents import safe_filename
from .auth import get_current_user
from .utils import get_project_for_org

router = APIRouter(tags=["upload"])
logger = logging.getLogger(__name__)

IFC_EXTENSIONS = {".ifc", ".ifczip", ".ifcxml"}
PDF_EXTENSIONS = {".pdf"}
DOCX_EXTENSIONS = {".docx"}
XML_EXTENSIONS = {".xml"}
REGISTRY_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".json", ".jsonl"}
RAG_EXTENSIONS = PDF_EXTENSIONS | DOCX_EXTENSIONS
UPLOAD_CHUNK_SIZE = 1024 * 1024
_PACKAGE_LOCKS: dict[str, asyncio.Lock] = {}


def _mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.0f} МБ"


def _max_file_size_message(max_size: int | None) -> str:
    limit = max_size if max_size is not None else settings.MAX_UPLOAD_FILE_SIZE_BYTES
    return f"Файл превышает максимально допустимый размер {_mb(limit)}"


def _max_package_size_message(max_size: int | None = None) -> str:
    limit = max_size if max_size is not None else settings.MAX_UPLOAD_PACKAGE_SIZE_BYTES
    return f"Суммарный размер пакета файлов превышает лимит {_mb(limit)}"


def _package_bytes_uploaded(db: Session, *, project_id: int, organization_id: int, process_id: str) -> int:
    """Sum of file_size for files already uploaded into the current, still-open
    verification cycle -- the closest existing concept in this system to a
    'package' of files a user is submitting together (ТЗ 9.1)."""
    total = (
        db.query(func.coalesce(func.sum(UploadJob.file_size), 0))
        .filter(
            UploadJob.project_id == int(project_id),
            UploadJob.organization_id == int(organization_id),
            UploadJob.process_id == str(process_id),
            UploadJob.status != "failed",
            UploadJob.file_size.isnot(None),
        )
        .scalar()
    )
    return int(total or 0)


def _detect_source_type(filename: str, requested_file_type: str | None) -> str:
    requested = (requested_file_type or "auto").strip().lower()
    suffix = PurePath(filename).suffix.lower()
    if requested in {"ifc", "ifczip", "ifcxml"} or suffix in IFC_EXTENSIONS:
        return "ifc"
    if requested == "xml" or suffix in XML_EXTENSIONS:
        return "xml"
    if requested == "registry" or suffix in REGISTRY_EXTENSIONS:
        return "registry"
    return "rag"


def _detect_rag_file_type(filename: str, requested_file_type: str | None) -> str:
    requested = (requested_file_type or "auto").strip().lower()
    suffix = PurePath(filename).suffix.lower()
    if requested in {"pdf", "docx"}:
        return requested
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    if suffix in DOCX_EXTENSIONS:
        return "docx"
    raise HTTPException(
        status_code=400,
        detail="Unsupported file format. Supported formats: PDF, DOCX, XML",
    )


async def _save_upload_file(file: UploadFile, path: Path, *, max_size: int | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.part")
    total = 0
    exceeded = False
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if max_size is not None and total > max_size:
                    exceeded = True
                    break
                out.write(chunk)
        if exceeded:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=413, detail=_max_file_size_message(max_size))
        if total <= 0:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Empty file")
        tmp_path.replace(path)
        return total
    except HTTPException:
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _job_upload_path(*, organization_id: int, project_id: int, job_id: int, filename: str,
                     process_id: str | None = None, source_type: str | None = None) -> Path:
    if process_id and (settings.OFFLINE_DELIVERY or source_type == "registry"):
        return (settings.BATCH_PACKAGES_ROOT / "_uploads" / str(project_id) / str(process_id)
                / "_incoming" / f"{job_id}_{filename}")
    return settings.TEMP_UPLOADS_DIR / f"org_{organization_id}" / str(project_id) / f"{job_id}_{filename}"


def _offline_package_dir(job: UploadJob) -> Path:
    root = settings.BATCH_PACKAGES_ROOT.resolve()
    package = (root / "_uploads" / str(int(job.project_id)) / str(job.process_id)).resolve()
    if not package.is_relative_to(root):
        raise ValueError("Upload package path escapes CASE10_BATCH_PACKAGES_ROOT")
    configured_originals = os.environ.get("CASE10_ORIGINALS_ROOT", "").strip()
    if configured_originals and Path(configured_originals).resolve() != root:
        raise ValueError("CASE10_ORIGINALS_ROOT must match CASE10_BATCH_PACKAGES_ROOT for offline UI uploads")
    os.environ["CASE10_ORIGINALS_ROOT"] = str(root)
    return package


def _package_upload_lock(process_id: str) -> asyncio.Lock:
    return _PACKAGE_LOCKS.setdefault(str(process_id), asyncio.Lock())


def _open_upload_process(db: Session, *, project_id: int, organization_id: int):
    """Keep sequential UI uploads in their existing open cycle, including after a registry supplies object_id."""
    from ..db.models import InspectionProcess

    recent = (
        db.query(UploadJob)
        .filter(
            UploadJob.project_id == int(project_id),
            UploadJob.organization_id == int(organization_id),
            UploadJob.process_id.isnot(None),
            UploadJob.status != "failed",
        )
        .order_by(UploadJob.id.desc())
        .first()
    )
    process = db.get(InspectionProcess, str(recent.process_id)) if recent else None
    if process is not None and str(process.status or "") != "FINALIZED":
        return process
    return get_or_create_open_process(db, project_id=int(project_id), organization_id=int(organization_id))


def _promote_offline_upload(job: UploadJob, temp_path: Path) -> tuple[Path, Path, Path]:
    package = _offline_package_dir(job)
    if str(job.source_type) == "registry":
        registry_dir = package / "registry"
        registry_dir.mkdir(parents=True, exist_ok=True)
        suffix = PurePath(str(job.filename)).suffix.lower()
        if suffix not in REGISTRY_EXTENSIONS:
            raise ValueError("Реестр должен быть CSV, XLSX, JSON или JSONL")
        source_resolved = temp_path.resolve()
        for prior in registry_dir.iterdir():
            if prior.is_file() and prior.resolve() != source_resolved:
                prior.unlink()
        destination = registry_dir / f"registry{suffix}"
    else:
        files_dir = package / "files" / str(int(job.id))
        files_dir.mkdir(parents=True, exist_ok=True)
        destination = files_dir / safe_filename(str(job.filename))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if temp_path.resolve() != destination.resolve():
        temp_path.replace(destination)
    return package, package / "files", destination


def _import_offline_upload_package(db: Session, job: UploadJob, package: Path, current_path: Path) -> tuple[Any, list[Any], dict[str, Any]]:
    scanned = bp.scan_package(package / "files") if (package / "files").is_dir() else []
    files = [bp.PackageFile(f"files/{item.relative_path}", item.size, item.sha256, item.pdf_pages) for item in scanned]
    registry_files = sorted((package / "registry").glob("registry.*")) if (package / "registry").is_dir() else []
    if len(registry_files) > 1:
        raise bp.PackageError("Загружено несколько реестров; оставьте один реестр Перечня ИД 1.1")
    registry_path = registry_files[0] if registry_files else None
    registry = bp.read_registry(registry_path) if registry_path else None
    if registry_path and not registry:
        raise bp.PackageError("В реестре нет строк документов")
    if not files:
        return None, [], {
            "registry_present": bool(registry_path),
            "registry_status": bp.STATUS_CLARIFICATION_REQUIRED if not registry_path else bp.STATUS_ACCEPTED,
            "registry_status_reason": "REGISTRY_MISSING" if not registry_path else None,
            "files_total": 0,
        }

    process = db.get(InspectionProcess, str(job.process_id))
    configured_object_id = str(getattr(process, "object_id", "") or "").strip() or None
    object_ids = sorted({str(row["object_id"]) for row in registry or [] if row.get("object_id")})
    if len(object_ids) > 1:
        raise bp.PackageError(f"Реестр содержит несколько object_id: {object_ids}")
    object_id = object_ids[0] if object_ids else (configured_object_id or
               f"UPLOAD-P{int(job.project_id)}-{hashlib.sha256(str(job.process_id).encode()).hexdigest()[:12]}")
    plan = bp.build_plan(
        files, registry, package_name=f"upload-{job.process_id}", object_id=object_id,
        registry_sha256=bp.sha256_file(registry_path) if registry_path else None,
        originals_prefix=package.relative_to(settings.BATCH_PACKAGES_ROOT.resolve()).as_posix(),
    )
    if not plan.rows:
        raise bp.PackageError("Не найдено документов для регистрации")
    for row in plan.rows:
        row["registry_required"] = True
        row["registry_status"] = plan.report["registry_status"]
        row["registry_status_reason"] = plan.report["registry_status_reason"]

    manifest_path = (settings.DATA_DIR / "upload_manifests" / str(int(job.project_id))
                     / f"{job.process_id}.jsonl")
    bp.import_plan(db, plan, project_id=int(job.project_id), organization_id=int(job.organization_id or 0),
                   manifest_path=manifest_path)
    if process is not None and process.object_id != plan.object_id:
        process.object_id = plan.object_id
        db.add(process)
    documents = (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.project_id == int(job.project_id),
            DocumentVersion.organization_id == int(job.organization_id or 0),
            DocumentVersion.object_id == plan.object_id,
            DocumentVersion.dataset_split == bp.PACKAGE_SPLIT,
        )
        .order_by(DocumentVersion.id.asc())
        .all()
    )
    current_rel = current_path.relative_to(package).as_posix()
    current_row = next((row for row in plan.rows if row.get("package_path") == current_rel), None)
    current_doc = next((doc for doc in documents if current_row and doc.dataset_file_id == current_row["file_id"]), None)
    if process is not None:
        db.add(AuditLog(
            user_id=int(job.user_id) if job.user_id is not None else None,
            action="CASE10_UPLOAD_PACKAGE_IMPORTED",
            object_id=plan.object_id,
            project_id=int(job.project_id),
            process_id=str(job.process_id),
            details={
                "upload_job_id": int(job.id),
                "registry_present": bool(registry_path),
                "registry_sha256": bp.sha256_file(registry_path) if registry_path else None,
                "report": plan.report,
            },
            ip_address=get_client_ip(),
        ))
    return current_doc, documents, plan.report


def _ensure_xml_source_fragment(db: Session, doc_version, file_path: Path) -> SourceFragment:
    existing = (
        db.query(SourceFragment)
        .filter(
            SourceFragment.document_version_id == int(doc_version.id),
            SourceFragment.source_system == "xml",
            SourceFragment.external_id == "xml:document",
        )
        .first()
    )
    try:
        text_value = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text_value = ""
    text_value = text_value[:6000]
    if existing:
        existing.text = text_value
        existing.page = 1
        existing.bbox = [0.0, 0.0, 1.0, 1.0]
        existing.metadata_json = {"filename": doc_version.filename, "source_type": "xml"}
        db.add(existing)
        db.flush()
        return existing
    fragment = SourceFragment(
        document_version_id=int(doc_version.id),
        page=1,
        bbox=[0.0, 0.0, 1.0, 1.0],
        text=text_value,
        fragment_type="xml",
        source_system="xml",
        external_id="xml:document",
        metadata_json={"filename": doc_version.filename, "source_type": "xml"},
        extractor="xml_text_reader",
        confidence=0.8,
    )
    db.add(fragment)
    db.flush()
    return fragment


async def _process_upload_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(UploadJob, int(job_id))
        if not job:
            return

        job.status = "processing"
        job.stage = "Передача файла в сервис индексации"
        job.progress = 20
        db.add(job)
        db.commit()

        temp_path = Path(str(job.temp_path or ""))
        if not temp_path.exists() or not temp_path.is_file():
            raise FileNotFoundError("Временный файл загрузки не найден")

        source_type = str(job.source_type or "rag")
        rag_file_type = _detect_rag_file_type(job.filename, job.file_type) if source_type == "rag" else None

        doc_version = None
        doc_changed = True
        package_documents = None
        offline_package = False
        case10_job = None
        if source_type == "ifc":
            result = await IfcClient(timeout=900.0).import_model(
                project_id=int(job.project_id),
                organization_id=int(job.organization_id or 0),
                filename=str(job.filename),
                file_path=temp_path,
            )
            job.document_id = int(result["model_id"])
            job.status = "queued"
            job.stage = "IFC-модель принята, индексирование поставлено в очередь"
            job.detail = "Дождитесь статуса indexed перед вопросами по IFC-модели"
        elif source_type == "xml":
            doc_version, doc_changed = ensure_document_version_for_upload_job(db, job, temp_path)
            if doc_changed:
                _ensure_xml_source_fragment(db, doc_version, temp_path)
            job.document_id = int(doc_version.id)
            job.status = "ready"
            job.stage = "XML сохранен в реестр документов"
            job.detail = "XML доступен для V3 parameter extraction" if doc_changed else "Файл идентичен уже загруженной версии"
            if doc_changed:
                case10_job = _recompute_case10_for_uploaded_document(db, job, doc_version)
        elif settings.OFFLINE_DELIVERY or source_type == "registry":
            offline_package = True
            job.stage = "Сохранение в офлайн-пакет CASE10"
            job.progress = 35
            db.add(job)
            db.commit()
            async with _package_upload_lock(str(job.process_id)):
                package, _files_dir, stored_path = _promote_offline_upload(job, temp_path)
                job.temp_path = str(stored_path)
                doc_version, package_documents, package_report = _import_offline_upload_package(
                    db, job, package, stored_path,
                )
                if not package_documents:
                    # The registry can arrive before the first drawing; retain it so the first document import uses
                    # this exact registry. A registry is metadata, not an evidence document.
                    registry_path = next(iter(sorted((package / "registry").glob("registry.*"))), None)
                    db.add(AuditLog(
                        user_id=int(job.user_id) if job.user_id is not None else None,
                        action="CASE10_UPLOAD_PACKAGE_IMPORTED",
                        object_id=package_report.get("object_id"),
                        project_id=int(job.project_id),
                        process_id=str(job.process_id),
                        details={
                            "upload_job_id": int(job.id),
                            "registry_present": bool(registry_path),
                            "registry_sha256": bp.sha256_file(registry_path) if registry_path else None,
                            "report": package_report,
                        },
                        ip_address=get_client_ip(),
                    ))
                job.document_id = int(doc_version.id) if doc_version is not None else None
                job.status = "ready"
                job.stage = "Файл зарегистрирован в офлайн-пакете CASE10"
                if source_type == "registry":
                    job.detail = (f"Реестр Перечня ИД 1.1 применён; документов: {len(package_documents)}; "
                                  f"статус: {package_report.get('registry_status')}")
                else:
                    job.detail = (f"Документ зарегистрирован через batch_package; SHA-256: "
                                  f"{(doc_version.file_hash or doc_version.content_hash) if doc_version else '—'}; "
                                  f"статус реестра: {package_report.get('registry_status')}")
                if package_documents:
                    case10_job = _recompute_case10_for_uploaded_documents(db, job, package_documents)
        else:
            client = RagClient(timeout=1800.0, retries=1)
            if rag_file_type == "pdf":
                result = await client.ingest_layout(
                    project_id=int(job.project_id),
                    organization_id=int(job.organization_id or 0),
                    filename=str(job.filename),
                    file_path=temp_path,
                )
                job.detail = (
                    f"Страниц: {result.get('pages_created', 0)}, "
                    f"фрагментов: {result.get('text_chunks_created', 0)}"
                )
            else:
                result = await client.ingest(
                    project_id=int(job.project_id),
                    organization_id=int(job.organization_id or 0),
                    file_type=str(rag_file_type),
                    filename=str(job.filename),
                    file_path=temp_path,
                )
                job.detail = f"Фрагментов: {result.get('chunks_created', 0)}"
            job.document_id = int(result["document_id"])
            job.status = "ready"
            job.stage = "Документ проиндексирован в RAG"

        if source_type != "xml" and not offline_package:
            doc_version, doc_changed = ensure_document_version_for_upload_job(db, job, temp_path)
            if doc_version is not None and doc_changed:
                case10_job = _recompute_case10_for_uploaded_document(db, job, doc_version)
            elif doc_version is not None and not doc_changed:
                job.detail = f"{job.detail or ''} Файл идентичен текущей версии — пересчёт CASE10 не требуется.".strip()
                db.add(AuditLog(
                    user_id=int(job.user_id) if job.user_id is not None else None,
                    action="CASE10_DUPLICATE_UPLOAD_SKIPPED",
                    object_id=doc_version.object_id,
                    project_id=int(job.project_id),
                    details={
                        "upload_job_id": int(job.id),
                        "document_version_id": int(doc_version.id),
                        "filename": job.filename,
                        "content_hash": doc_version.content_hash,
                    },
                    ip_address=get_client_ip(),
                ))
        job.progress = 100
        job.finished_at = datetime.utcnow()
        db.add(job)
        db.commit()
        if case10_job is not None:
            published = publish_job_message(case10_job)
            if settings.OFFLINE_DELIVERY and not published:
                try:
                    result = await asyncio.to_thread(_execute_case10_job_inline, dict(case10_job.payload_json or {}))
                    logger.info("CASE10 offline inline execution | job=%s process=%s outcome=%s",
                                case10_job.id, case10_job.process_id, result.outcome)
                except Exception as exc:
                    logger.exception("CASE10 offline inline execution failed | job=%s: %s", case10_job.id, exc)
                    job.detail = f"{job.detail or ''} CASE10 inline failed: {type(exc).__name__}: {exc}".strip()
                    db.add(job)
                    db.commit()
        logger.info(
            "Upload indexed | job=%s source=%s object_id=%s file=%s",
            job.id,
            source_type,
            job.document_id,
            job.filename,
        )
    except Exception as exc:
        logger.exception("Upload indexing failed | job=%s: %s", job_id, exc)
        try:
            job = db.get(UploadJob, int(job_id))
            if job:
                job.status = "failed"
                job.stage = "Ошибка индексации файла"
                job.progress = 100
                job.detail = f"{type(exc).__name__}: {exc}"
                job.finished_at = datetime.utcnow()
                db.add(job)
                db.commit()
        except Exception:
            logger.exception("Failed to persist failed upload job state | job=%s", job_id)
    finally:
        db.close()


def _recompute_case10_for_uploaded_document(db: Session, job: UploadJob, doc_version):
    return _recompute_case10_for_uploaded_documents(db, job, [doc_version])


def _execute_case10_job_inline(payload: dict):
    """Run the queued job in a worker thread when offline demo mode has no broker/consumer."""
    db = SessionLocal()
    try:
        return execute_process_job(db, payload)
    finally:
        db.close()


def _recompute_case10_for_uploaded_documents(db: Session, job: UploadJob, documents: list[DocumentVersion]):
    try:
        process = db.get(InspectionProcess, str(job.process_id)) if job.process_id else None
        if process is None:
            process = get_or_create_open_process(
                db,
                project_id=int(job.project_id),
                organization_id=int(job.organization_id or 0),
            )
        impact_scope = impact_scope_for_documents(db, process, documents)
        affected = impact_scope["param_codes"]
        process.affected_param_codes = affected
        db.add(process)
        db.flush()
        if affected:
            case10_job = enqueue_process_job(
                db,
                process,
                user_id=int(job.user_id) if job.user_id is not None else None,
                affected_param_codes=affected,
                reason="upload_incremental",
                impact_scope=impact_scope,
            )
            job.detail = f"{job.detail or ''} CASE10: поставлены в очередь параметры {', '.join(affected)}".strip()
            return case10_job
    except Exception as exc:
        logger.exception("CASE10 incremental recompute failed | job=%s document_versions=%s: %s", job.id,
                         [getattr(document, "id", None) for document in documents], exc)
        job.detail = f"{job.detail or ''} CASE10 recompute failed: {type(exc).__name__}: {exc}".strip()
    return None


@router.post("/upload", response_model=UploadResponse)
async def upload(
    background_tasks: BackgroundTasks,
    project_id: int = Form(...),
    file_type: str = Form("auto"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload a project file and route it to RAG or IFC indexing."""
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")

    get_project_for_org(db, project_id, user.organization_id)

    original_filename = safe_filename(file.filename or "file")
    source_type = _detect_source_type(original_filename, file_type)
    rag_file_type = _detect_rag_file_type(original_filename, file_type) if source_type == "rag" else None

    declared_size = int(file.size) if file.size is not None else 0
    if declared_size > settings.MAX_UPLOAD_FILE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail=_max_file_size_message(settings.MAX_UPLOAD_FILE_SIZE_BYTES))

    process = _open_upload_process(
        db,
        project_id=int(project_id),
        organization_id=int(user.organization_id),
    )
    db.commit()

    package_existing = _package_bytes_uploaded(
        db,
        project_id=int(project_id),
        organization_id=int(user.organization_id),
        process_id=str(process.id),
    )
    if package_existing + declared_size > settings.MAX_UPLOAD_PACKAGE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail=_max_package_size_message(settings.MAX_UPLOAD_PACKAGE_SIZE_BYTES))

    job = UploadJob(
        project_id=project_id,
        organization_id=user.organization_id,
        user_id=user.id,
        filename=original_filename,
        file_type=file_type if file_type and file_type != "auto" else (rag_file_type or source_type),
        temp_path=None,
        process_id=str(process.id),
        source_type=source_type,
        status="queued",
        stage="Файл принят, обработка поставлена в очередь",
        progress=5,
        detail="Файл сохраняется на сервер",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    temp_path = _job_upload_path(
        organization_id=int(user.organization_id),
        project_id=int(project_id),
        job_id=int(job.id),
        filename=original_filename,
        process_id=str(process.id),
        source_type=source_type,
    )
    try:
        file_size = await _save_upload_file(file, temp_path, max_size=settings.MAX_UPLOAD_FILE_SIZE_BYTES)
    except Exception as exc:
        job.status = "failed"
        job.stage = "Ошибка сохранения файла"
        job.progress = 100
        job.detail = f"{type(exc).__name__}: {exc}"
        job.finished_at = datetime.utcnow()
        db.add(job)
        db.commit()
        raise
    job.temp_path = str(temp_path)
    job.file_size = file_size
    job.detail = f"Размер файла: {file_size} байт"
    db.add(job)
    db.commit()

    # The offline delivery has no RAG worker to pick queued uploads up. Registry files also
    # have to be imported immediately so the next PDF upload can use their manifest rows.
    if (settings.AUTO_PROCESS_UPLOADS
            or (settings.OFFLINE_DELIVERY and source_type not in {"xml", "ifc"})
            or source_type == "registry"):
        background_tasks.add_task(_process_upload_job, int(job.id))

    return UploadResponse(
        upload_id=int(job.id),
        process_id=str(process.id),
        filename=original_filename,
        status=str(job.status),
        stage=job.stage,
        progress=int(job.progress or 0),
        source_type=source_type,
    )


@router.post("/upload-jobs/{job_id}/process", response_model=UploadResponse)
async def process_upload_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")
    get_project_for_org(db, project_id, user.organization_id)

    job = (
        db.query(UploadJob)
        .filter(
            UploadJob.id == int(job_id),
            UploadJob.project_id == int(project_id),
            UploadJob.organization_id == int(user.organization_id),
        )
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Upload job not found")
    if str(job.status or "") == "processing":
        raise HTTPException(status_code=409, detail="Upload job is already processing")
    if not job.temp_path or not Path(job.temp_path).exists():
        raise HTTPException(status_code=404, detail="Uploaded file not found for this job")

    job.status = "queued"
    job.stage = "Обработка поставлена в очередь"
    job.progress = 5
    job.detail = job.detail or "Файл ожидает обработки"
    job.finished_at = None
    db.add(job)
    db.commit()

    background_tasks.add_task(_process_upload_job, int(job.id))

    return UploadResponse(
        upload_id=int(job.id),
        process_id=None,
        filename=str(job.filename),
        status=str(job.status),
        stage=job.stage,
        progress=int(job.progress or 0),
        source_type=str(job.source_type or "rag"),
    )
