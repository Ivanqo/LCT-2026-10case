from __future__ import annotations

import io
import json
import mimetypes
import re
import shutil
import zipfile
from pathlib import Path, PurePath
from typing import Any
from xml.etree import ElementTree as ET

from fastapi import HTTPException
from pypdf import PdfReader, PdfWriter
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import ProjectDocument

CONTROL_CHARS = {chr(i) for i in range(32)} | {chr(127)}
MAX_SUMMARY_CHARS = 3_500


def safe_filename(name: str) -> str:
    base = PurePath((name or "file").replace("\\", "/")).name.strip()
    cleaned = "".join("_" if ch in CONTROL_CHARS else ch for ch in base)
    cleaned = cleaned.replace("/", "_").replace("\\", "_").replace("\x00", "_").strip(" .")
    return cleaned or "file"


def safe_dirname(value: str) -> str:
    value = (value or "item").strip().lower()
    value = re.sub(r"[^a-zа-я0-9._-]+", "_", value, flags=re.IGNORECASE).strip("._-")
    return value or "item"


def unique_path(folder: Path, filename: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(filename)
    path = folder / filename
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = folder / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def project_folder(organization_id: int, project_id: int) -> Path:
    return settings.PROJECT_FILES_DIR / f"org_{organization_id}" / f"project_{project_id}"


def originals_folder(organization_id: int, project_id: int) -> Path:
    return project_folder(organization_id, project_id) / "original"


def meta_folder(organization_id: int, project_id: int) -> Path:
    return project_folder(organization_id, project_id) / "meta"


def guess_content_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext in {".txt", ".md", ".csv"}:
        return "text/plain"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def detect_file_type(filename: str, declared: str | None = None) -> str:
    declared = (declared or "").strip().lower()
    if declared and declared not in {"auto", "file"}:
        return declared
    ext = Path(filename).suffix.lower().lstrip(".")
    return ext or "file"


def _compact_text(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _extract_pdf_first_page_text(path: Path) -> tuple[str, int]:
    reader = PdfReader(str(path))
    pages_count = len(reader.pages)
    text = ""
    if pages_count:
        try:
            text = reader.pages[0].extract_text() or ""
        except Exception:
            text = ""
    if not text.strip():
        text = _try_ocr_first_pdf_page(path)
    return _compact_text(text), pages_count


def _try_ocr_first_pdf_page(path: Path) -> str:
    """Optional OCR. Works only if pdf2image+pytesseract+system binaries are installed."""
    if not settings.ENABLE_OPTIONAL_OCR:
        return ""
    try:
        from pdf2image import convert_from_path  # type: ignore
        import pytesseract  # type: ignore

        images = convert_from_path(str(path), first_page=1, last_page=1, dpi=180)
        if not images:
            return ""
        return pytesseract.image_to_string(images[0], lang=settings.OCR_LANG).strip()
    except Exception:
        return ""


def _extract_docx_text(path: Path) -> tuple[str, int]:
    texts: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
        root = ET.fromstring(xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        for node in root.findall(".//w:t", ns):
            if node.text:
                texts.append(node.text)
    except Exception:
        return "", 1
    return _compact_text(" ".join(texts)), 1


def extract_document_summary(path: Path, file_type: str) -> tuple[str, int]:
    ext = path.suffix.lower()
    try:
        if ext == ".pdf" or file_type == "pdf":
            return _extract_pdf_first_page_text(path)
        if ext == ".docx" or file_type == "docx":
            return _extract_docx_text(path)
        if ext in {".txt", ".md", ".csv"}:
            return _compact_text(path.read_text(encoding="utf-8", errors="ignore")), 1
    except Exception:
        return "", 0
    return "", 0


def create_simple_document(
    db: Session,
    *,
    organization_id: int,
    project_id: int,
    user_id: int | None,
    filename: str,
    file_type: str | None,
    src_path: Path,
) -> ProjectDocument:
    final_path = unique_path(originals_folder(organization_id, project_id), filename)
    shutil.move(str(src_path), str(final_path))
    detected_type = detect_file_type(final_path.name, file_type)
    first_page_text, pages_count = extract_document_summary(final_path, detected_type)
    if not first_page_text:
        first_page_text = "Текст на первой странице не найден. Возможен скан без текстового слоя или неподдерживаемый формат."

    meta = {
        "filename": final_path.name,
        "file_type": detected_type,
        "pages_count": int(pages_count or 0),
        "first_page_text": first_page_text,
        "relative_path": str(final_path.relative_to(settings.PROJECT_FILES_DIR)),
    }
    meta_path = unique_path(meta_folder(organization_id, project_id), f"{final_path.stem}.meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    doc = ProjectDocument(
        project_id=project_id,
        organization_id=organization_id,
        user_id=user_id,
        filename=final_path.name,
        file_type=detected_type,
        storage_path=str(final_path),
        meta_path=str(meta_path),
        pages_count=int(pages_count or 0),
        first_page_text=first_page_text,
        status="ready",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def list_project_documents(db: Session, *, organization_id: int, project_id: int) -> list[ProjectDocument]:
    return (
        db.query(ProjectDocument)
        .filter(ProjectDocument.organization_id == organization_id, ProjectDocument.project_id == project_id)
        .order_by(ProjectDocument.created_at.desc())
        .all()
    )


def get_document_for_org(db: Session, *, document_id: int, organization_id: int, project_id: int | None = None) -> ProjectDocument:
    q = db.query(ProjectDocument).filter(ProjectDocument.id == document_id, ProjectDocument.organization_id == organization_id)
    if project_id is not None:
        q = q.filter(ProjectDocument.project_id == project_id)
    doc = q.first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


def document_to_item(doc: ProjectDocument) -> dict[str, Any]:
    return {
        "id": int(doc.id),
        "project_id": int(doc.project_id),
        "filename": str(doc.filename),
        "uploaded_at": doc.created_at,
        "source_type": "simple",
        "status": str(doc.status or "ready"),
        "stage": "Файл сохранён, доступен предпросмотр",
        "progress": 100,
        "detail": f"Страниц: {int(doc.pages_count or 0)}" if int(doc.pages_count or 0) else None,
        "processing_status": str(doc.status or "ready"),
        "processing_progress": 100,
        "is_ready": True,
        "error_message": None,
    }


def make_simple_page_id(document_id: int, page_number: int) -> int:
    return document_id * 100000 + max(1, int(page_number))


def parse_simple_page_id(page_id: int) -> tuple[int, int]:
    document_id = int(page_id) // 100000
    page_number = int(page_id) % 100000
    return document_id, max(1, page_number)


def render_pdf_page_image(doc: ProjectDocument, page_number: int) -> tuple[bytes, str]:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=501, detail=f"PDF page rendering requires PyMuPDF: {exc}")
    path = Path(doc.storage_path)
    if path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Page preview is supported only for PDF")
    pdf = fitz.open(str(path))
    try:
        index = max(0, min(int(page_number) - 1, len(pdf) - 1))
        page = pdf.load_page(index)
        pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
        return pix.tobytes("png"), "image/png"
    finally:
        pdf.close()


def build_pages_pdf(db: Session, *, organization_id: int, page_ids: list[int]) -> tuple[bytes, str, str]:
    grouped: dict[int, list[int]] = {}
    for page_id in page_ids:
        doc_id, page_number = parse_simple_page_id(page_id)
        grouped.setdefault(doc_id, []).append(page_number)
    writer = PdfWriter()
    output_name = "selected_pages.pdf"
    for doc_id, page_numbers in grouped.items():
        doc = get_document_for_org(db, document_id=doc_id, organization_id=organization_id)
        path = Path(doc.storage_path)
        if path.suffix.lower() != ".pdf":
            continue
        reader = PdfReader(str(path))
        output_name = f"{Path(doc.filename).stem}_selected_pages.pdf"
        for page_number in sorted(set(page_numbers)):
            idx = page_number - 1
            if 0 <= idx < len(reader.pages):
                writer.add_page(reader.pages[idx])
    if len(writer.pages) == 0:
        raise HTTPException(status_code=404, detail="No PDF pages found")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), "application/pdf", output_name
