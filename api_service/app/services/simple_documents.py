from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Iterable
from xml.etree import ElementTree as ET

import httpx
from fastapi import HTTPException
from pypdf import PdfReader, PdfWriter
from sqlalchemy.orm import Session

from ..clients import require_external_service
from ..config import settings
from ..db.models import ProjectDocument

CONTROL_CHARS = {chr(i) for i in range(32)} | {chr(127)}
UPLOAD_MAX_BYTES = 10_000_000
MAX_STAGE1_FILES = 80
MAX_SUMMARY_CHARS = 3_500
MAX_VISUAL_PAGES = int(os.getenv("MAX_VISUAL_PAGES", "24"))
PDF_PAGE_IMAGE_ZOOM = float(os.getenv("PDF_PAGE_IMAGE_ZOOM", "1.25"))
logger = logging.getLogger(__name__)

DRAWING_QUERY_RX = re.compile(r"(схем|черт[её]ж|узел|детал|витраж|фасад|план|разрез|лист|маркировк)", flags=re.IGNORECASE | re.UNICODE)


@dataclass(frozen=True)
class UploadPart:
    name: str
    payload: bytes
    content_type: str
    original_filename: str
    start_page: int | None = None
    end_page: int | None = None


PLANNER_SYSTEM_PROMPT = """
Ты помощник для отбора проектной документации.
Тебе дают вопрос пользователя и список файлов проекта с короткими описаниями первой страницы.
Верни только валидный JSON без markdown-блока.

JSON-схема:
{
  "needed_files": ["точное имя файла 1", "точное имя файла 2"],
  "thinking_instruction": "краткая инструкция для следующего шага: какие документы читать, какой анализ/расчёты выполнить, как проверять источники"
}

Правила:
- Выбирай только файлы из списка.
- Если по названиям/summary непонятно, выбери 3-7 наиболее вероятных файлов.
- Для вопросов про схемы/чертежи/витражи/узлы/фасады обязательно выбирай архитектурные/рабочие PDF, даже если на первой странице нет нужного текста.
- Не придумывай имена файлов.
- thinking_instruction пиши как инструкцию для модели, но без раскрытия скрытых рассуждений.
""".strip()

ANSWER_SYSTEM_PROMPT = """
Ты senior BIM/строительный AI-ассистент для анализа проектной и рабочей документации.
Работай только по прикреплённым файлам, манифесту файлов и инструкции.
Не выдумывай факты. Если данных недостаточно — прямо скажи, каких документов/листов не хватает.

КРИТИЧНО ДЛЯ PDF, РАЗБИТЫХ НА ЧАСТИ:
- В промпте есть манифест: какая временная часть соответствует каким исходным страницам.
- Во всех ссылках указывай ИСХОДНОЕ имя файла, а не имя временной части.
- Номер страницы в ссылке должен быть номером страницы исходного PDF.
- Нельзя ссылаться на страницу больше pages_count из манифеста.
- Если страница внутри части неочевидна, лучше укажи источник без страницы, чем придумывай номер.

Если пользователь просит схемы, чертежи, витражи, узлы, планы или фасады:
- анализируй PDF визуально, а не только текстовый слой;
- ищи на листах графические обозначения, марки, узлы, экспликации и подписи;
- если объект найден, перечисли страницы/листы-кандидаты и приложи источники;
- не пиши «отсутствует», пока не проверены все прикреплённые части нужного PDF.

Формат ответа — аккуратный Markdown:
### Краткий ответ
1–3 предложения.

### Что найдено
Маркированный список найденных документов/листов/схем.

### Обоснование
Коротко объясни, по каким признакам сделан вывод.

### Источники
Список ссылок.

Ссылки на источники обязательно оформляй в явном виде:
[[source: точное_имя_файла.pdf, page: 12]]
Если страницу определить нельзя:
[[source: точное_имя_файла.pdf]]
""".strip()

_SOURCE_RX = re.compile(
    r"\[\[\s*source\s*:\s*(?P<file>[^,\]]+?)\s*(?:,\s*page\s*:\s*(?P<page>\d+))?\s*\]\]",
    flags=re.IGNORECASE | re.UNICODE,
)


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
        "stage": "Файл сохранён, готов к LLM-анализу",
        "progress": 100,
        "detail": f"Страниц: {int(doc.pages_count or 0)}" if int(doc.pages_count or 0) else None,
        "processing_status": str(doc.status or "ready"),
        "processing_progress": 100,
        "is_ready": True,
        "error_message": None,
    }


async def _proxy_upload_bytes(filename: str, content: bytes, content_type: str) -> dict[str, Any]:
    require_external_service("Qwen-прокси")
    timeout_seconds = max(float(settings.QWEN_TIMEOUT), 300.0)
    timeout = httpx.Timeout(connect=15.0, read=timeout_seconds, write=timeout_seconds, pool=15.0)
    url = f"{settings.QWEN_PROXY_BASE_URL}/files/upload"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, files={"file": (filename, content, content_type)})
            response_text = response.text
            if response.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM proxy /files/upload вернул HTTP {response.status_code}: {response_text[:1200]}",
                )
            try:
                data = response.json()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"LLM proxy /files/upload вернул не JSON: {exc}; body={response_text[:1200]}")
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"Таймаут при загрузке файла в LLM proxy: {exc}")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось связаться с LLM proxy /files/upload: {exc}")

    return _build_file_obj(data, filename_fallback=filename, content_type_fallback=content_type)


def _first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _build_file_obj(upload_resp: dict[str, Any], *, filename_fallback: str, content_type_fallback: str) -> dict[str, Any]:
    """Normalize /files/upload response into the richest possible Qwen file descriptor.

    Different proxy versions return different shapes:
    - {file: {name, url, size, type}}
    - {file: {file_id, file_path, file_url, ...}, ...}
    - {success, fileId, filePath, url, stsData: {...}}
    We intentionally keep duplicate camelCase/snake_case fields because Qwen Web payloads
    have changed several times.
    """
    if not isinstance(upload_resp, dict):
        upload_resp = {}

    file_node = upload_resp.get("file") if isinstance(upload_resp.get("file"), dict) else {}
    sts_node = upload_resp.get("stsData") if isinstance(upload_resp.get("stsData"), dict) else {}

    filename = _first_value(
        upload_resp.get("filename"),
        upload_resp.get("fileName"),
        upload_resp.get("file_name"),
        file_node.get("filename"),
        file_node.get("name"),
        file_node.get("file_name"),
        sts_node.get("filename"),
        filename_fallback,
    )

    file_id = _first_value(
        upload_resp.get("file_id"),
        upload_resp.get("fileId"),
        upload_resp.get("id"),
        file_node.get("file_id"),
        file_node.get("fileId"),
        file_node.get("id"),
        sts_node.get("file_id"),
        sts_node.get("fileId"),
        sts_node.get("id"),
    )

    file_path = _first_value(
        upload_resp.get("file_path"),
        upload_resp.get("filePath"),
        upload_resp.get("path"),
        file_node.get("file_path"),
        file_node.get("filePath"),
        file_node.get("path"),
        sts_node.get("file_path"),
        sts_node.get("filePath"),
        sts_node.get("path"),
    )

    file_url = _first_value(
        upload_resp.get("file_url"),
        upload_resp.get("fileUrl"),
        upload_resp.get("url"),
        file_node.get("file_url"),
        file_node.get("fileUrl"),
        file_node.get("url"),
        sts_node.get("file_url"),
        sts_node.get("fileUrl"),
        sts_node.get("url"),
    )

    size = _first_value(
        upload_resp.get("size"),
        upload_resp.get("filesize"),
        upload_resp.get("fileSize"),
        file_node.get("size"),
        file_node.get("filesize"),
        sts_node.get("filesize"),
    )

    content_type = _first_value(
        upload_resp.get("content_type"),
        upload_resp.get("mime_type"),
        upload_resp.get("file_type"),
        upload_resp.get("type"),
        file_node.get("content_type"),
        file_node.get("mime_type"),
        file_node.get("file_type"),
        file_node.get("type"),
        content_type_fallback,
    )

    try:
        size = int(size) if size is not None else None
    except Exception:
        size = None

    # Stop early: if proxy uploaded to local server only and did not return OSS/Qwen descriptor,
    # model will not see the attachment.
    if not any([file_id, file_path, file_url]):
        raise HTTPException(
            status_code=502,
            detail=(
                "LLM proxy /files/upload не вернул file_id/file_path/file_url/url. "
                "Файл загружен на proxy, но не прикреплён к Qwen."
            ),
        )

    out: dict[str, Any] = {
        "type": "file",
        "id": file_id,
        "uid": file_id,
        "file_id": file_id,
        "fileId": file_id,
        "name": filename,
        "filename": filename,
        "file_name": filename,
        "url": file_url,
        "file_url": file_url,
        "fileUrl": file_url,
        "path": file_path,
        "file_path": file_path,
        "filePath": file_path,
        "size": size,
        "filesize": size,
        "file_type": content_type,
        "content_type": content_type,
        "mime_type": content_type,
        "status": "uploaded",
        "upload_status": "success",
        "file": {
            "filename": filename,
            "meta": {
                "name": filename,
                "size": size,
                "content_type": content_type,
                "file_id": file_id,
                "file_path": file_path,
                "file_url": file_url,
            },
        },
    }
    # Remove only None values, keep nested structure readable.
    return {k: v for k, v in out.items() if v is not None}



def _messages_have_files(messages: list[dict[str, Any]]) -> bool:
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("files") or msg.get("attachments"):
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"file", "file_url", "image_url"}:
                    return True
    return False


def _try_parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _format_proxy_error(value: Any) -> str:
    value = _try_parse_json_string(value)
    if not isinstance(value, dict):
        return str(value)
    data = value.get("data") if isinstance(value.get("data"), dict) else None
    target = data or value
    code = target.get("code") or target.get("type")
    message = target.get("message") or target.get("details") or target.get("detail") or target.get("error")
    message = _try_parse_json_string(message)
    if isinstance(message, dict):
        message = _format_proxy_error(message)
    if code and message:
        text = f"{code}: {message}"
    elif message:
        text = str(message)
    else:
        text = json.dumps(value, ensure_ascii=False)
    request_id = value.get("request_id") or value.get("requestId")
    if request_id:
        text = f"{text} (request_id={request_id})"
    return text


def _extract_proxy_error(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    if data.get("error"):
        return _format_proxy_error(data.get("error"))
    if data.get("success") is False:
        return _format_proxy_error(data)
    has_answer = bool(data.get("choices") or data.get("answer") or data.get("content") or data.get("message") or data.get("text"))
    if data.get("details") and not has_answer:
        details = _try_parse_json_string(data.get("details"))
        if isinstance(details, dict):
            nested = _extract_proxy_error(details)
            return nested or _format_proxy_error(details)
        return str(details)
    return None


async def _proxy_chat(messages: list[dict[str, Any]], *, chat_id: str | None = None, use_chat_endpoint: bool = False) -> tuple[str, str | None]:
    require_external_service("Qwen-прокси")
    # ВАЖНО: /api/chat в старых версиях Qwen-proxy игнорировал files.
    # Поэтому любой запрос с файлами отправляем строго в /api/chat/completions.
    has_files = _messages_have_files(messages)
    endpoint = "/chat/completions" if has_files or not use_chat_endpoint else "/chat"

    payload: dict[str, Any] = {"model": settings.QWEN_MODEL, "messages": messages, "stream": False}
    if chat_id:
        payload["chatId"] = chat_id
        payload["chat_id"] = chat_id

    # Файлы оставляем только внутри user-message.files.
    # Не дублируем их в top-level files/attachments: Qwen-proxy сам достанет их из messages,
    # а дублирование приводило к 3x количеству файлов и Internal error у Qwen.

    # Qwen-file requests can be long: the proxy may keep the SSE connection open
    # for 1-3 minutes while it reads PDF attachments. Do not let backend timeout
    # earlier than the proxy/model. We intentionally clamp to at least 15 min
    # even if the old env value QWEN_TIMEOUT=60/120/300 is still set in Docker.
    timeout_seconds = max(float(settings.QWEN_TIMEOUT), 900.0)
    timeout = httpx.Timeout(connect=15.0, read=timeout_seconds, write=timeout_seconds, pool=15.0)
    url = f"{settings.QWEN_PROXY_BASE_URL}{endpoint}"
    logger.info("qwen_proxy_request_start endpoint=%s has_files=%s timeout=%s chat_id=%s", endpoint, has_files, timeout_seconds, chat_id)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            response_text = response.text
            logger.info("qwen_proxy_response endpoint=%s status=%s bytes=%s", endpoint, response.status_code, len(response_text or ""))
            if response.status_code >= 400 and endpoint == "/chat":
                url = f"{settings.QWEN_PROXY_BASE_URL}/chat/completions"
                response = await client.post(url, json=payload)
                response_text = response.text
                logger.info("qwen_proxy_fallback_response endpoint=/chat/completions status=%s bytes=%s", response.status_code, len(response_text or ""))

            if response.status_code >= 400:
                detail = response_text[:2500]
                try:
                    err_json = response.json()
                    detail = json.dumps(err_json, ensure_ascii=False)[:2500]
                except Exception:
                    pass
                raise HTTPException(status_code=502, detail=f"LLM proxy {endpoint} вернул HTTP {response.status_code}: {detail}")

            try:
                data = response.json()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"LLM proxy {endpoint} вернул не JSON: {exc}; body={response_text[:1200]}")
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        logger.exception("qwen_proxy_timeout endpoint=%s timeout=%s chat_id=%s", endpoint, timeout_seconds, chat_id)
        raise HTTPException(
            status_code=504,
            detail=(
                f"Backend дождался таймаута ответа от LLM proxy {endpoint}. "
                f"Текущий лимит ожидания: {timeout_seconds:.0f} сек. "
                "Проверь логи proxy: если ответ Qwen пришёл позже этого времени, увеличь QWEN_TIMEOUT. "
                f"Технические детали: {exc}"
            ),
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось связаться с LLM proxy {endpoint}: {exc}")

    proxy_error = _extract_proxy_error(data)
    if proxy_error:
        raise HTTPException(status_code=502, detail=f"LLM proxy returned error: {proxy_error}")

    if isinstance(data, dict) and data.get("error"):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or json.dumps(err, ensure_ascii=False)
        else:
            msg = str(err)
        raise HTTPException(status_code=502, detail=f"LLM proxy вернул ошибку: {msg}")

    answer = _extract_answer(data)
    if not answer.strip():
        raise HTTPException(
            status_code=502,
            detail=(
                "LLM proxy вернул пустой ответ от модели. "
                "Проверь формат attachments в proxy logs: в Qwen PAYLOAD должны быть files/attachments, "
                "а raw/debug не должен быть пустым."
            ),
        )
    return answer, _extract_chat_id(data) or chat_id


async def _create_proxy_chat(name: str) -> str | None:
    require_external_service("Qwen-прокси")
    timeout_seconds = max(float(settings.QWEN_TIMEOUT), 300.0)
    timeout = httpx.Timeout(connect=15.0, read=timeout_seconds, write=timeout_seconds, pool=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{settings.QWEN_PROXY_BASE_URL}/chats", json={"name": name, "model": settings.QWEN_MODEL})
            response.raise_for_status()
            data = response.json()
        return data.get("chatId") or data.get("chat_id") or data.get("id")
    except Exception as exc:
        logger.warning("Не удалось создать отдельный чат LLM: %r", exc)
        return None


def _extract_answer(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)
    try:
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        pass
    return str(data.get("answer") or data.get("content") or data.get("message") or data.get("text") or data).strip()


def _extract_chat_id(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get("chatId") or data.get("chat_id") or data.get("id")
    return str(value) if value else None


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return {}


def _stage1_file_list(docs: Iterable[ProjectDocument]) -> str:
    rows: list[str] = []
    for idx, doc in enumerate(docs, start=1):
        summary = _compact_text(doc.first_page_text or "", 1200)
        rows.append(
            f"{idx}. filename: {doc.filename}\n"
            f"   type: {doc.file_type}\n"
            f"   pages_count: {doc.pages_count or 0}\n"
            f"   first_page_summary: {summary}"
        )
    return "\n\n".join(rows)


def _select_docs_by_names(docs: list[ProjectDocument], names: Iterable[str]) -> list[ProjectDocument]:
    by_name = {str(d.filename).strip().lower(): d for d in docs}
    selected: list[ProjectDocument] = []
    for name in names or []:
        key = str(name or "").strip().lower()
        if key in by_name and by_name[key] not in selected:
            selected.append(by_name[key])
    if selected:
        return selected
    # fallback: first documents if LLM returned invalid names
    return docs[: min(5, len(docs))]


def _split_binary_bytes(filename: str, content: bytes) -> list[UploadPart]:
    ctype = guess_content_type(filename)
    if len(content) <= UPLOAD_MAX_BYTES:
        return [UploadPart(filename, content, ctype, filename)]
    parts: list[UploadPart] = []
    base, ext = os.path.splitext(filename)
    for idx, start in enumerate(range(0, len(content), UPLOAD_MAX_BYTES), start=1):
        parts.append(UploadPart(f"{base}_part{idx:03d}{ext}", content[start : start + UPLOAD_MAX_BYTES], ctype, filename))
    return parts


def _split_pdf_bytes(filename: str, content: bytes) -> list[UploadPart]:
    try:
        reader = PdfReader(io.BytesIO(content))
        total_pages = len(reader.pages)
    except Exception:
        return _split_binary_bytes(filename, content)

    if len(content) <= UPLOAD_MAX_BYTES:
        return [UploadPart(filename, content, "application/pdf", filename, 1, total_pages or None)]

    parts: list[UploadPart] = []
    base, ext = os.path.splitext(filename)
    current_page_indexes: list[int] = []
    part_idx = 1

    def build_pdf(indexes: list[int]) -> bytes:
        writer = PdfWriter()
        for page_index in indexes:
            writer.add_page(reader.pages[page_index])
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    for idx in range(total_pages):
        candidate_indexes = current_page_indexes + [idx]
        candidate_payload = build_pdf(candidate_indexes)
        if len(candidate_payload) > UPLOAD_MAX_BYTES and current_page_indexes:
            payload = build_pdf(current_page_indexes)
            first_page = current_page_indexes[0] + 1
            last_page = current_page_indexes[-1] + 1
            parts.append(
                UploadPart(
                    f"{base}_part{part_idx:03d}_pages_{first_page:03d}-{last_page:03d}{ext}",
                    payload,
                    "application/pdf",
                    filename,
                    first_page,
                    last_page,
                )
            )
            part_idx += 1
            current_page_indexes = [idx]
        else:
            current_page_indexes = candidate_indexes

    if current_page_indexes:
        payload = build_pdf(current_page_indexes)
        first_page = current_page_indexes[0] + 1
        last_page = current_page_indexes[-1] + 1
        parts.append(
            UploadPart(
                f"{base}_part{part_idx:03d}_pages_{first_page:03d}-{last_page:03d}{ext}",
                payload,
                "application/pdf",
                filename,
                first_page,
                last_page,
            )
        )

    if any(len(part.payload) > UPLOAD_MAX_BYTES for part in parts):
        return _split_binary_bytes(filename, content)
    return parts


def build_upload_parts(doc: ProjectDocument) -> list[UploadPart]:
    path = Path(doc.storage_path)
    payload = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        return _split_pdf_bytes(doc.filename, payload)
    return _split_binary_bytes(doc.filename, payload)


async def upload_docs_to_llm(docs: list[ProjectDocument]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    file_objs: list[dict[str, Any]] = []
    sent_parts: list[dict[str, Any]] = []
    for doc in docs:
        for part in build_upload_parts(doc):
            info = {
                "part_name": part.name,
                "original_filename": part.original_filename,
                "start_page": part.start_page,
                "end_page": part.end_page,
                "size": len(part.payload),
            }
            file_obj = await _proxy_upload_bytes(part.name, part.payload, part.content_type)
            info["qwen_file_id"] = file_obj.get("file_id")
            info["qwen_file_path"] = file_obj.get("file_path")
            info["qwen_file_url"] = file_obj.get("file_url")
            sent_parts.append(info)
            file_objs.append(file_obj)
    return file_objs, sent_parts


def _render_pdf_page_png(path: Path, page_number: int) -> bytes:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=501, detail=f"Для визуального анализа PDF нужен PyMuPDF: {exc}")

    pdf = fitz.open(str(path))
    try:
        if page_number < 1 or page_number > len(pdf):
            raise HTTPException(status_code=400, detail=f"Страница {page_number} вне диапазона PDF")
        page = pdf.load_page(page_number - 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(PDF_PAGE_IMAGE_ZOOM, PDF_PAGE_IMAGE_ZOOM), alpha=False)
        return pix.tobytes("png")
    finally:
        pdf.close()


async def upload_docs_pages_as_images_to_llm(docs: list[ProjectDocument]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Визуальный режим для чертежей: отправляем страницы PDF как image URL.

    Qwen Web API нестабильно принимает PDF в message.files, а examples прокси показывают
    рабочий путь для визуального анализа через content: [{type:'image', image:url}].
    Поэтому для запросов про схемы/чертежи/витражи рендерим страницы в PNG и отправляем как изображения.
    """
    image_objs: list[dict[str, Any]] = []
    sent_parts: list[dict[str, Any]] = []
    used_pages = 0

    for doc in docs:
        if used_pages >= MAX_VISUAL_PAGES:
            break
        path = Path(doc.storage_path)
        if path.suffix.lower() != ".pdf":
            continue
        pages_count = int(doc.pages_count or 0)
        if pages_count <= 0:
            try:
                pages_count = len(PdfReader(str(path)).pages)
            except Exception:
                pages_count = 0
        for page_number in range(1, pages_count + 1):
            if used_pages >= MAX_VISUAL_PAGES:
                break
            image_bytes = _render_pdf_page_png(path, page_number)
            image_name = f"{Path(doc.filename).stem}_page_{page_number:03d}.png"
            image_obj = await _proxy_upload_bytes(image_name, image_bytes, "image/png")
            info = {
                "part_name": image_name,
                "original_filename": doc.filename,
                "start_page": page_number,
                "end_page": page_number,
                "size": len(image_bytes),
                "qwen_file_id": image_obj.get("file_id"),
                "qwen_file_path": image_obj.get("file_path"),
                "qwen_file_url": image_obj.get("file_url") or image_obj.get("url"),
                "visual_mode": True,
            }
            sent_parts.append(info)
            image_objs.append(image_obj)
            used_pages += 1
    return image_objs, sent_parts


def _image_content_parts(prompt: str, image_objs: list[dict[str, Any]], sent_parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_obj, part in zip(image_objs, sent_parts):
        url = image_obj.get("file_url") or image_obj.get("url") or image_obj.get("fileUrl")
        if not url:
            continue
        label = (
            f"\n\nИзображение ниже — страница {part.get('start_page')} "
            f"исходного файла {part.get('original_filename')}. "
            f"При ссылке используй [[source: {part.get('original_filename')}, page: {part.get('start_page')}]]."
        )
        content.append({"type": "text", "text": label})
        content.append({"type": "image", "image": str(url)})
    return content


def parse_sources(answer: str, docs: list[ProjectDocument]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    docs_by_name = {d.filename.lower(): d for d in docs}
    source_rows: list[dict[str, Any]] = []
    page_rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for match in _SOURCE_RX.finditer(answer or ""):
        filename = match.group("file").strip()
        page = int(match.group("page") or 0)
        doc = docs_by_name.get(filename.lower())
        if not doc:
            # try loose basename match
            doc = next((d for d in docs if Path(d.filename).name.lower() == Path(filename).name.lower()), None)
        if not doc:
            stem = re.sub(r"_part\d+$", "", Path(filename).stem, flags=re.IGNORECASE)
            suffix = Path(filename).suffix.lower()
            normalized_part_name = f"{stem}{suffix}".lower()
            doc = docs_by_name.get(normalized_part_name)
        if not doc:
            continue
        page_number = page if page > 0 else 1
        key = (int(doc.id), int(page_number))
        if key in seen:
            continue
        seen.add(key)
        page_id = make_simple_page_id(int(doc.id), int(page_number))
        row = {
            "chunk_id": None,
            "page_id": page_id,
            "document_id": int(doc.id),
            "filename": doc.filename,
            "page_number": page_number,
            "text": "Источник указан моделью в ответе",
            "region_id": None,
            "score": None,
            "selected": True,
            "related_asset_ids": [],
        }
        source_rows.append(row)
        page_rows.append({"page_id": page_id, "document_id": int(doc.id), "filename": doc.filename, "page_number": page_number})
    return source_rows, page_rows


def make_simple_page_id(document_id: int, page_number: int) -> int:
    return document_id * 100000 + max(1, int(page_number))


def parse_simple_page_id(page_id: int) -> tuple[int, int]:
    document_id = int(page_id) // 100000
    page_number = int(page_id) % 100000
    return document_id, max(1, page_number)


def _build_file_manifest(docs: list[ProjectDocument], sent_parts: list[dict[str, Any]]) -> str:
    by_file: dict[str, list[dict[str, Any]]] = {}
    for part in sent_parts:
        by_file.setdefault(str(part.get("original_filename") or ""), []).append(part)

    rows: list[str] = []
    for doc in docs:
        rows.append(f"- original_filename: {doc.filename}")
        rows.append(f"  pages_count: {int(doc.pages_count or 0)}")
        for part in by_file.get(doc.filename, []):
            if part.get("start_page") and part.get("end_page"):
                rows.append(
                    f"  attached_part: {part['part_name']} -> original pages "
                    f"{part['start_page']}-{part['end_page']}"
                )
            else:
                rows.append(f"  attached_part: {part.get('part_name')} -> original file/page range unknown")
    return "\n".join(rows)


def _normalize_answer_citations(answer: str, docs: list[ProjectDocument], sent_parts: list[dict[str, Any]]) -> str:
    docs_by_name = {d.filename.lower(): d for d in docs}
    part_to_original: dict[str, tuple[str, int | None, int | None]] = {}
    for part in sent_parts:
        part_to_original[str(part.get("part_name") or "").lower()] = (
            str(part.get("original_filename") or ""),
            part.get("start_page"),
            part.get("end_page"),
        )

    def repl(match: re.Match[str]) -> str:
        raw_file = match.group("file").strip()
        raw_page = int(match.group("page") or 0)
        filename = raw_file
        page = raw_page

        part_info = part_to_original.get(raw_file.lower())
        if part_info:
            original, start_page, end_page = part_info
            filename = original or filename
            if page > 0 and start_page:
                converted = int(start_page) + page - 1
                if not end_page or converted <= int(end_page):
                    page = converted

        doc = docs_by_name.get(filename.lower())
        if not doc:
            stem = re.sub(r"_part\d+(?:_pages_\d+-\d+)?$", "", Path(filename).stem, flags=re.IGNORECASE)
            candidate = f"{stem}{Path(filename).suffix}".lower()
            doc = docs_by_name.get(candidate)
            if doc:
                filename = doc.filename

        if not doc:
            return f"[[source: {filename}]]"

        pages_count = int(doc.pages_count or 0)
        if page > 0 and (pages_count <= 0 or page <= pages_count):
            return f"[[source: {doc.filename}, page: {page}]]"
        return f"[[source: {doc.filename}]]"

    return _SOURCE_RX.sub(repl, answer or "")


def _is_drawing_query(question: str) -> bool:
    return bool(DRAWING_QUERY_RX.search(question or ""))


def _expand_docs_for_drawing_query(all_docs: list[ProjectDocument], selected_docs: list[ProjectDocument]) -> list[ProjectDocument]:
    if not selected_docs:
        selected_docs = []
    selected_ids = {int(d.id) for d in selected_docs}
    out = list(selected_docs)
    # Для графических запросов лучше отправить больше PDF-кандидатов: первая страница часто не содержит слов "витраж"/"схема".
    preferred_markers = ("ар", "ас", "кж", "км", "фасад", "витраж", "окн", "ал", "черт", "схем")
    pdf_docs = [d for d in all_docs if str(d.filename).lower().endswith(".pdf")]
    preferred = [d for d in pdf_docs if any(m in str(d.filename).lower() for m in preferred_markers)]
    candidates = preferred or pdf_docs
    for doc in candidates[:8]:
        if int(doc.id) not in selected_ids:
            out.append(doc)
            selected_ids.add(int(doc.id))
    return out[:10]


async def run_simple_llm_flow(
    db: Session,
    *,
    organization_id: int,
    project_id: int,
    user_question: str,
) -> dict[str, Any]:
    docs = list_project_documents(db, organization_id=organization_id, project_id=project_id)
    docs = [d for d in docs if str(d.status or "ready") == "ready"]
    if not docs:
        raise HTTPException(status_code=404, detail="В проекте нет загруженных файлов")
    docs_for_stage1 = docs[:MAX_STAGE1_FILES]

    chat_id = await _create_proxy_chat(f"PD/RD question: {user_question[:60]}")

    planner_prompt = (
        "ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n"
        f"{user_question}\n\n"
        "СПИСОК ФАЙЛОВ ПРОЕКТА:\n"
        f"{_stage1_file_list(docs_for_stage1)}\n\n"
        "Верни JSON с needed_files и thinking_instruction."
    )
    planner_answer, chat_id = await _proxy_chat(
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": planner_prompt},
        ],
        chat_id=chat_id,
    )
    planner_json = _extract_json_object(planner_answer)
    needed_files = planner_json.get("needed_files") if isinstance(planner_json.get("needed_files"), list) else []
    instruction = str(planner_json.get("thinking_instruction") or "Проанализируй прикреплённые документы и ответь строго по источникам.").strip()
    selected_docs = _select_docs_by_names(docs, needed_files)
    if _is_drawing_query(user_question):
        selected_docs = _expand_docs_for_drawing_query(docs, selected_docs)

    # ВАЖНО ДЛЯ MVP: документы отправляем именно файлами, а не постраничными PNG.
    # Если файл <= 10 МБ — отправляется целиком. Если > 10 МБ — делится на PDF-части
    # по диапазонам страниц. Рендер страниц в картинки оставлен как helper для просмотра
    # листов в UI, но в LLM-flow по умолчанию не используется.
    visual_mode = False
    file_objs, sent_parts = await upload_docs_to_llm(selected_docs)
    if not file_objs:
        raise HTTPException(status_code=500, detail="Не удалось прикрепить файлы к запросу LLM")

    answer_prompt = (
        "ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n"
        f"{user_question}\n\n"
        "ИНСТРУКЦИЯ ДЛЯ АНАЛИЗА:\n"
        f"{instruction}\n\n"
        "ПРИКРЕПЛЁННЫЕ ФАЙЛЫ:\n"
        + _build_file_manifest(selected_docs, sent_parts)
        + "\n\n"
        "Ответь по прикреплённым файлам. В каждом существенном утверждении указывай источник в формате "
        "[[source: исходное_имя_файла.pdf, page: N]]. Используй только страницы из манифеста выше. "
        "Если документ был отправлен частями, переведи страницу части в исходную страницу по диапазону attached_part. "
        "Никогда не указывай страницу больше pages_count. "
        "Оформи ответ в Markdown с разделами: ### Краткий ответ, ### Что найдено, ### Обоснование, ### Источники."
    )
    if _is_drawing_query(user_question):
        answer_prompt += (
            "\n\nДОПОЛНИТЕЛЬНО ДЛЯ ЧЕРТЕЖЕЙ/СХЕМ:\n"
            "- Проверь визуальное содержимое всех прикреплённых PDF-файлов или PDF-частей выбранных файлов.\n"
            "- Ищи нужный объект не только по тексту, но и по графическим листам/маркам/экспликациям.\n"
            "- Если точная схема не подписана словом из вопроса, перечисли ближайшие листы-кандидаты и объясни почему.\n"
        )

    user_message: dict[str, Any] = {"role": "user", "content": answer_prompt, "files": file_objs}
    answer, chat_id = await _proxy_chat(
        messages=[
            {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
            user_message,
        ],
        chat_id=chat_id,
    )
    answer = _normalize_answer_citations(answer, selected_docs, sent_parts)
    sources, pages = parse_sources(answer, selected_docs)
    if not sources:
        # fallback: attach first page of selected docs so UI still shows evidence candidates.
        for doc in selected_docs[:3]:
            page_id = make_simple_page_id(int(doc.id), 1)
            sources.append(
                {
                    "chunk_id": None,
                    "page_id": page_id,
                    "document_id": int(doc.id),
                    "filename": doc.filename,
                    "page_number": 1,
                    "text": (doc.first_page_text or "")[:1200],
                    "region_id": None,
                    "score": None,
                    "selected": True,
                    "related_asset_ids": [],
                }
            )
            pages.append({"page_id": page_id, "document_id": int(doc.id), "filename": doc.filename, "page_number": 1})

    return {
        "answer": answer,
        "sources": sources,
        "pages": pages,
        "selected_docs": selected_docs,
        "planner": planner_json,
        "chat_id": chat_id,
        "sent_part_names": [str(p.get("part_name")) for p in sent_parts],
        "sent_part_details": sent_parts,
    }


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
