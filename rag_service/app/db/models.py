from __future__ import annotations

import json
from sqlalchemy import Column, Integer, String, DateTime, Text, func, Index, ForeignKey
from sqlalchemy.orm import relationship

from .session import Base


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, nullable=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    filename = Column(String(512), nullable=False)
    stored_path = Column(String(2048), nullable=True)
    uploaded_at = Column(DateTime, nullable=False, server_default=func.now())
    status = Column(String(32), nullable=False, default="processing", server_default="processing", index=True)
    stage = Column(String(255), nullable=True)
    progress = Column(Integer, nullable=False, default=0, server_default="0")
    detail = Column(Text, nullable=True)
    processing_started_at = Column(DateTime, nullable=True)
    processing_finished_at = Column(DateTime, nullable=True)

    chunks = relationship("Chunk", back_populates="document", cascade="all, delete-orphan")
    pages = relationship("Page", back_populates="document", cascade="all, delete-orphan")


Index("ix_documents_org_project_uploaded_at", Document.organization_id, Document.project_id, Document.uploaded_at.desc())


class Page(Base):
    __tablename__ = "pages"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, nullable=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    page_number = Column(Integer, nullable=False, index=True)
    width = Column(Integer, nullable=False, default=0)
    height = Column(Integer, nullable=False, default=0)
    image_path = Column(String(2048), nullable=True)
    text_raw = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    document = relationship("Document", back_populates="pages")
    regions = relationship("Region", back_populates="page", cascade="all, delete-orphan")


Index("ix_pages_doc_page", Page.document_id, Page.page_number)


class Region(Base):
    __tablename__ = "regions"

    id = Column(Integer, primary_key=True, index=True)
    page_id = Column(Integer, ForeignKey("pages.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, nullable=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    region_type = Column(String(64), nullable=False, index=True)
    bbox_json = Column(Text, nullable=False, default="[]")
    ocr_text = Column(Text, nullable=True)
    data_json = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    image_path = Column(String(2048), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    page = relationship("Page", back_populates="regions")

    @property
    def bbox(self) -> list[int]:
        try:
            return json.loads(self.bbox_json or "[]")
        except Exception:
            return []

    def set_bbox(self, bbox: list[int]) -> None:
        self.bbox_json = json.dumps([int(x) for x in bbox])


Index("ix_regions_page_type", Region.page_id, Region.region_type)


class Chunk(Base):
    __tablename__ = "chunks"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, nullable=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    page_number = Column(Integer, nullable=False, default=0)
    text = Column(Text, nullable=False)
    region_id = Column(Integer, nullable=True, index=True)
    embedding_id = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    document = relationship("Document", back_populates="chunks")
