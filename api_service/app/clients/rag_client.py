from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import settings
from . import require_external_service

logger = logging.getLogger(__name__)


class RagClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 120.0, retries: int = 4, retry_delay: float = 1.5):
        require_external_service("RAG-сервис")
        self.base_url = (base_url or settings.RAG_SERVICE_URL).rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.retry_delay = max(0.0, retry_delay)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.request(method, url, **kwargs)
                    response.raise_for_status()
                    return response
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt >= self.retries:
                    break
                logger.warning(
                    "RAG request retry %s/%s failed for %s %s: %s",
                    attempt,
                    self.retries,
                    method,
                    url,
                    exc,
                )
                await asyncio.sleep(self.retry_delay * attempt)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"RAG request failed unexpectedly: {method} {url}")

    async def ingest(
        self,
        *,
        project_id: int,
        organization_id: int | None,
        file_type: str,
        filename: str,
        file_bytes: bytes | None = None,
        file_path: str | Path | None = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/ingest"
        data = {
            "project_id": project_id,
            "organization_id": organization_id or 0,
            "file_type": file_type,
            "original_filename": filename,
        }
        if file_path is not None:
            with Path(file_path).open("rb") as fh:
                files = {"file": (filename, fh)}
                r = await self._request("POST", url, data=data, files=files)
        else:
            if file_bytes is None:
                raise ValueError("file_bytes or file_path is required")
            files = {"file": (filename, file_bytes)}
            r = await self._request("POST", url, data=data, files=files)
        return r.json()

    async def ingest_layout(
        self,
        *,
        project_id: int,
        organization_id: int | None,
        filename: str,
        file_bytes: bytes | None = None,
        file_path: str | Path | None = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/ingest_layout"
        data = {
            "project_id": project_id,
            "organization_id": organization_id or 0,
            "original_filename": filename,
        }
        if file_path is not None:
            with Path(file_path).open("rb") as fh:
                files = {"file": (filename, fh)}
                r = await self._request("POST", url, data=data, files=files)
        else:
            if file_bytes is None:
                raise ValueError("file_bytes or file_path is required")
            files = {"file": (filename, file_bytes)}
            r = await self._request("POST", url, data=data, files=files)
        return r.json()

    async def ask(self, *, project_id: int, organization_id: int | None, question: str, top_k: int) -> Dict[str, Any]:
        url = f"{self.base_url}/ask"
        r = await self._request("POST", url, json={"project_id": project_id, "organization_id": organization_id, "question": question, "top_k": top_k})
        return r.json()

    async def list_documents(self, *, project_id: int, organization_id: int | None) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/documents"
        r = await self._request("GET", url, params={"project_id": project_id, "organization_id": organization_id})
        return r.json()

    async def delete_document(self, *, document_id: int, organization_id: int | None) -> Dict[str, Any]:
        url = f"{self.base_url}/documents/{document_id}"
        r = await self._request("DELETE", url, params={"organization_id": organization_id})
        return r.json()

    async def reindex_document(self, *, document_id: int, project_id: int, organization_id: int | None) -> Dict[str, Any]:
        url = f"{self.base_url}/documents/{document_id}/reindex"
        r = await self._request("POST", url, params={"project_id": project_id, "organization_id": organization_id})
        return r.json()

    async def download_document(self, *, document_id: int, organization_id: int | None) -> Tuple[bytes, str, Dict[str, str]]:
        r = await self._request("GET", f"{self.base_url}/documents/{document_id}/download", params={"organization_id": organization_id})
        return r.content, (r.headers.get("content-type") or "application/octet-stream"), dict(r.headers)

    async def fetch_region_image(self, *, region_id: int, organization_id: int | None) -> Tuple[bytes, str]:
        r = await self._request("GET", f"{self.base_url}/layout/region_image", params={"region_id": region_id, "organization_id": organization_id})
        return r.content, (r.headers.get("content-type") or "image/png")

    async def fetch_page_image(self, *, page_id: int, organization_id: int | None) -> Tuple[bytes, str]:
        r = await self._request("GET", f"{self.base_url}/layout/page_image", params={"page_id": page_id, "organization_id": organization_id})
        return r.content, (r.headers.get("content-type") or "image/png")

    async def download_page(self, *, page_id: int, organization_id: int | None) -> Tuple[bytes, str, Dict[str, str]]:
        r = await self._request("GET", f"{self.base_url}/layout/page_download", params={"page_id": page_id, "organization_id": organization_id})
        return r.content, (r.headers.get("content-type") or "application/octet-stream"), dict(r.headers)

    async def download_pages_pdf(self, *, page_ids: list[int], organization_id: int | None) -> Tuple[bytes, str, Dict[str, str]]:
        params = {"organization_id": organization_id, "page_ids": ",".join(str(int(x)) for x in page_ids if x)}
        r = await self._request("GET", f"{self.base_url}/layout/pages_pdf", params=params)
        return r.content, (r.headers.get("content-type") or "application/pdf"), dict(r.headers)

    async def export_source_fragments(self, *, document_id: int, organization_id: int | None) -> List[Dict[str, Any]]:
        r = await self._request(
            "GET",
            f"{self.base_url}/layout/source_fragments",
            params={"document_id": int(document_id), "organization_id": organization_id},
        )
        return r.json()
