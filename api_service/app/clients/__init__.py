from __future__ import annotations

from fastapi import HTTPException

from ..config import settings


def require_external_service(name: str) -> None:
    """Refuse, before any connection is attempted, to call a legacy service (RAG / IFC) that the offline CASE10
    delivery does not ship (CASE10_OFFLINE_DELIVERY=1, see app/config.py). Callers that treat the service as
    optional already catch the exception; the rest answer 503 instead of hanging on a connect timeout."""
    if settings.OFFLINE_DELIVERY:
        raise HTTPException(status_code=503, detail=f"{name} отключён в офлайн-поставке CASE10 (CASE10_OFFLINE_DELIVERY=1)")
