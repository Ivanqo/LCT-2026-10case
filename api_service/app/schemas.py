from __future__ import annotations

from datetime import datetime
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field
# Проверка пуша

class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)


class AuthTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "MeOut"


class MeOut(BaseModel):
    id: int
    login: str
    email: Optional[str] = None
    is_admin: bool
    role: str = "INSPECTOR"
    organization_id: Optional[int]
    organization_name: Optional[str] = None

    class Config:
        from_attributes = True


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)


class OrganizationOut(BaseModel):
    id: int
    name: str
    slug: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    login: str = Field(min_length=3, max_length=255)
    email: Optional[str] = Field(default=None, max_length=255)
    organization_id: int
    is_admin: bool = False
    role: Optional[str] = Field(default=None, pattern="^(INSPECTOR|ADMIN|ML_ENGINEER|SUPERVISOR)$")
    password: Optional[str] = Field(default=None, min_length=8, max_length=255)


class UserBatchCreate(BaseModel):
    organization_id: int
    count: int = Field(ge=1, le=200)
    login_prefix: str = Field(min_length=3, max_length=50)
    is_admin: bool = False
    role: Optional[str] = Field(default=None, pattern="^(INSPECTOR|ADMIN|ML_ENGINEER|SUPERVISOR)$")


class UserOut(BaseModel):
    id: int
    login: str
    email: Optional[str] = None
    api_key: str
    is_admin: bool
    role: str = "INSPECTOR"
    organization_id: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


class UserCredentialOut(UserOut):
    password: str


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    description: Optional[str] = Field(default=None, max_length=5000)


class ProjectOut(BaseModel):
    id: int
    organization_id: Optional[int]
    name: str
    description: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class UploadResponse(BaseModel):
    upload_id: int
    process_id: Optional[str] = None
    filename: str
    status: str
    stage: Optional[str] = None
    progress: int = 0
    source_type: str = "rag"


class SourceAssetRef(BaseModel):
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


class SourceRef(BaseModel):
    chunk_id: Optional[int] = None
    page_id: Optional[int] = None
    document_id: int
    filename: str
    page_number: int
    text: str = ""
    region_id: Optional[int] = None
    score: Optional[float] = None
    selected: bool = False
    related_asset_ids: List[int] = Field(default_factory=list)


class PageViewerRef(BaseModel):
    page_id: int
    document_id: int
    filename: str
    page_number: int


class IfcSourceRef(BaseModel):
    model_id: int
    file_name: str
    summary: str
    relevance: Optional[float] = None
    row_data: Dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    project_id: int
    question: str = Field(min_length=1)
    user_label: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=20)


class ChatResponse(BaseModel):
    answer: str
    selected_chunk_ids: List[int] = Field(default_factory=list)
    selected_asset_ids: List[int] = Field(default_factory=list)
    pages: List[PageViewerRef] = Field(default_factory=list)
    sources: List[SourceRef] = Field(default_factory=list)
    assets: List[SourceAssetRef] = Field(default_factory=list)
    ifc_sources: List[IfcSourceRef] = Field(default_factory=list)
    retrieval_mode: str = "documents+ifc"


class HistoryItem(BaseModel):
    id: int
    project_id: int
    user_id: Optional[int] = None
    question: str
    answer: str
    created_at: datetime
    user_label: Optional[str] = None
    references: Optional[str] = None

    class Config:
        from_attributes = True




class AdminQuestionOut(BaseModel):
    id: int
    organization_id: Optional[int] = None
    organization_name: Optional[str] = None
    project_id: int
    project_name: str
    user_id: Optional[int] = None
    user_login: Optional[str] = None
    user_email: Optional[str] = None
    user_label: Optional[str] = None
    question: str
    answer_preview: str = ""
    created_at: datetime


class AdminQuestionListOut(BaseModel):
    items: List[AdminQuestionOut]
    total: int


class DocumentItem(BaseModel):
    id: int
    project_id: int
    filename: str
    uploaded_at: datetime
    source_type: str = "rag"
    status: Optional[str] = None
    stage: Optional[str] = None
    progress: Optional[int] = None
    detail: Optional[str] = None
    processing_status: Optional[str] = None
    processing_progress: Optional[int] = None
    is_ready: Optional[bool] = None
    error_message: Optional[str] = None

    class Config:
        from_attributes = True


class DeleteDocumentOut(BaseModel):
    ok: bool = True
    source_type: str
    document_id: int
    deleted_text_embeddings: int = 0
    deleted_asset_embeddings: int = 0
    deleted_files: int = 0


class ReindexDocumentOut(BaseModel):
    ok: bool = True
    source_type: str = "rag"
    document_id: int
    pages_created: int = 0
    chunks_created: int = 0
    old_chunks_deleted: int = 0


class UserCredentialRecordOut(BaseModel):
    id: int
    user_id: int
    organization_id: Optional[int] = None
    login: str
    email: Optional[str] = None
    password: Optional[str] = None
    api_key: str
    is_admin: bool
    created_by_user_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class UserCredentialRecordListOut(BaseModel):
    items: List[UserCredentialRecordOut]
    total: int
