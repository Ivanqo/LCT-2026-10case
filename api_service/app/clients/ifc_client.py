from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ..config import settings
from . import require_external_service


class IfcClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 120.0):
        require_external_service("IFC-сервис")
        self.base_url = (base_url or settings.IFC_SERVICE_URL).rstrip("/")
        self.timeout = timeout

    async def import_model(
        self,
        *,
        project_id: int,
        organization_id: int | None,
        filename: str,
        file_bytes: bytes | None = None,
        file_path: str | Path | None = None,
    ) -> Dict[str, Any]:
        data = {
            "project_id": project_id,
            "organization_id": organization_id or 0,
            "original_filename": filename,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if file_path is not None:
                with Path(file_path).open("rb") as fh:
                    files = {"file": (filename, fh)}
                    response = await client.post(f"{self.base_url}/models/import", data=data, files=files)
            else:
                if file_bytes is None:
                    raise ValueError("file_bytes or file_path is required")
                files = {"file": (filename, file_bytes)}
                response = await client.post(f"{self.base_url}/models/import", data=data, files=files)
            response.raise_for_status()
            return response.json()

    async def status(self, model_id: int) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/models/{model_id}/status")
            response.raise_for_status()
            return response.json()

    async def list_models(
        self,
        *,
        project_id: int,
        organization_id: int | None,
    ) -> List[Dict[str, Any]]:
        params = {"project_id": project_id, "organization_id": organization_id}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/models", params=params)
            response.raise_for_status()
            return response.json()

    async def query_heavy(self, *, model_id: int, query_spec: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"model_id": model_id, "query_spec": query_spec}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/query/heavy", json=payload)
            response.raise_for_status()
            return response.json()

    async def get_context(
        self,
        *,
        project_id: int,
        organization_id: int | None,
        question: str,
        max_models: int = 3,
        limit: int = 15,
    ) -> Dict[str, Any]:
        payload = {
            "project_id": project_id,
            "organization_id": organization_id,
            "question": question,
            "max_models": max_models,
            "limit": limit,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/query/context", json=payload)
            response.raise_for_status()
            return response.json()

    async def export_observations(
        self,
        *,
        model_id: int,
        organization_id: int | None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        params = {"organization_id": organization_id, "limit": int(limit)}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/models/{model_id}/observations", params=params)
            response.raise_for_status()
            return response.json()

    async def delete_model(
        self,
        *,
        model_id: int,
        organization_id: int | None,
    ) -> Dict[str, Any]:
        params = {"organization_id": organization_id}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.delete(f"{self.base_url}/models/{model_id}", params=params)
            response.raise_for_status()
            return response.json()
