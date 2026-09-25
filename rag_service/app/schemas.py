from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class IngestResponse(BaseModel):
    document_id: int
    chunks_created: int


class LayoutIngestResponse(BaseModel):
    document_id: int
    pages_created: int
    regions_created: int
    text_chunks_created: int
    asset_records_created: int


class AskRequest(BaseModel):
    project_id: int
    organization_id: Optional[int] = None
    question: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


class SourceAsset(BaseModel):
    region_id: int
    page_id: Optional[int] = None
    document_id: int
    filename: str
    page_number: int
    region_type: str
    summary: Optional[str] = None
    ocr_text: Optional[str] = None
    bbox: List[int] = Field(default_factory=list)
    score: Optional[float] = None
    selected: bool = False
    has_image: bool = False
    linked_chunk_ids: List[int] = Field(default_factory=list)


class SourceChunk(BaseModel):
    chunk_id: Optional[int] = None
    page_id: Optional[int] = None
    document_id: int
    filename: str
    page_number: int
    text: str
    region_id: Optional[int] = None
    score: Optional[float] = None
    selected: bool = False
    related_asset_ids: List[int] = Field(default_factory=list)


class PageViewerItem(BaseModel):
    page_id: int
    document_id: int
    filename: str
    page_number: int


class AskResponse(BaseModel):
    answer: str
    selected_chunk_ids: List[int] = Field(default_factory=list)
    selected_asset_ids: List[int] = Field(default_factory=list)
    pages: List[PageViewerItem] = Field(default_factory=list)
    sources: List[SourceChunk] = Field(default_factory=list)
    assets: List[SourceAsset] = Field(default_factory=list)


class DocumentItem(BaseModel):
    id: int
    project_id: int
    filename: str
    uploaded_at: datetime
    source_type: str = "rag"
    status: str = "ready"
    stage: Optional[str] = None
    progress: int = 100
    detail: Optional[str] = None

    class Config:
        from_attributes = True


class DeleteDocumentResponse(BaseModel):
    ok: bool = True
    document_id: int
    deleted_text_embeddings: int = 0
    deleted_asset_embeddings: int = 0
    deleted_files: int = 0


class ReindexDocumentResponse(BaseModel):
    ok: bool = True
    document_id: int
    pages_created: int = 0
    chunks_created: int = 0
    old_chunks_deleted: int = 0


class PageItem(BaseModel):
    id: int
    document_id: int
    page_number: int
    width: int
    height: int
    image_path: Optional[str] = None

    class Config:
        from_attributes = True


class RegionItem(BaseModel):
    id: int
    page_id: int
    region_type: str
    bbox: List[int]
    summary: Optional[str] = None
    ocr_text: Optional[str] = None
    image_path: Optional[str] = None

    class Config:
        from_attributes = True


class SourceFragmentExportItem(BaseModel):
    source_system: str = "rag"
    external_id: str
    fragment_type: str
    page: Optional[int] = None
    bbox: List[int] = Field(default_factory=list)
    text: str = ""
    extractor: str = "rag_service"
    confidence: Optional[float] = None
    metadata: dict = Field(default_factory=dict)


class AssetSearchRequest(BaseModel):
    project_id: int
    organization_id: Optional[int] = None
    query: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=30)
    region_types: Optional[List[str]] = None


class AssetSearchHit(BaseModel):
    region_id: int
    document_id: int
    filename: str
    page_number: int
    region_type: str
    bbox: List[int]
    summary: Optional[str] = None
    score: Optional[float] = None


class AssetSearchResponse(BaseModel):
    hits: List[AssetSearchHit] = Field(default_factory=list)
