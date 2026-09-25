from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from ..clients.rag_client import RagClient
from ..db.models import User
from .auth import get_current_user

router = APIRouter(tags=["rag-assets"])


def _require_org(user: User) -> int:
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")
    return int(user.organization_id)


@router.get("/rag/region-image")
async def rag_region_image(region_id: int, user: User = Depends(get_current_user)):
    organization_id = _require_org(user)
    data, media_type = await RagClient().fetch_region_image(
        region_id=region_id,
        organization_id=organization_id,
    )
    return Response(content=data, media_type=media_type)


@router.get("/rag/page-image")
async def rag_page_image(page_id: int, user: User = Depends(get_current_user)):
    organization_id = _require_org(user)
    data, media_type = await RagClient().fetch_page_image(
        page_id=page_id,
        organization_id=organization_id,
    )
    return Response(content=data, media_type=media_type)


@router.get("/rag/page-download")
async def rag_page_download(page_id: int, user: User = Depends(get_current_user)):
    organization_id = _require_org(user)
    data, media_type, headers = await RagClient().download_page(
        page_id=page_id,
        organization_id=organization_id,
    )
    response = Response(content=data, media_type=media_type)
    if headers.get("content-disposition"):
        response.headers["Content-Disposition"] = headers["content-disposition"]
    return response


@router.get("/rag/pages-pdf")
async def rag_pages_pdf(page_ids: str, user: User = Depends(get_current_user)):
    organization_id = _require_org(user)
    parsed: list[int] = []
    for raw in str(page_ids or "").split(","):
        try:
            value = int(raw.strip())
        except Exception:
            continue
        if value > 0 and value not in parsed:
            parsed.append(value)
    if not parsed:
        raise HTTPException(status_code=400, detail="No page ids provided")

    data, media_type, headers = await RagClient().download_pages_pdf(
        page_ids=parsed,
        organization_id=organization_id,
    )
    response = Response(content=data, media_type=media_type)
    if headers.get("content-disposition"):
        response.headers["Content-Disposition"] = headers["content-disposition"]
    return response
