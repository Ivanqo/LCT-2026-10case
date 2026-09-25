from __future__ import annotations

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Float, Text, func, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .session import Base


class Model(Base):
    __tablename__ = "models"

    model_id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, nullable=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    original_filename = Column(String(512), nullable=False)
    stored_path = Column(String(2048), nullable=False)
    file_hash = Column(String(64), nullable=False, index=True)
    schema = Column(String(64), nullable=True)
    units = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="queued")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    indexed_at = Column(DateTime, nullable=True)

    elements = relationship("Element", back_populates="model", cascade="all, delete-orphan")


Index("ix_models_org_project_created_at", Model.organization_id, Model.project_id, Model.created_at.desc())


class Element(Base):
    __tablename__ = "elements"

    element_id = Column(Integer, primary_key=True, index=True)
    model_id = Column(Integer, ForeignKey("models.model_id", ondelete="CASCADE"), nullable=False, index=True)
    guid = Column(String(64), nullable=False, index=True)
    ifc_type = Column(String(128), nullable=False, index=True)
    name = Column(String(512), nullable=True)
    storey_guid = Column(String(64), nullable=True, index=True)
    container_guid = Column(String(64), nullable=True, index=True)

    model = relationship("Model", back_populates="elements")
    properties = relationship("Property", back_populates="element", cascade="all, delete-orphan")
    quantities = relationship("Quantity", back_populates="element", cascade="all, delete-orphan")
    metrics = relationship("Metric", back_populates="element", cascade="all, delete-orphan")


class Property(Base):
    __tablename__ = "properties"
    id = Column(Integer, primary_key=True, index=True)
    element_id = Column(Integer, ForeignKey("elements.element_id", ondelete="CASCADE"), nullable=False, index=True)
    pset = Column(String(256), nullable=True, index=True)
    name = Column(String(256), nullable=False, index=True)
    value_text = Column(Text, nullable=True)
    value_num = Column(Float, nullable=True)
    value_bool = Column(String(8), nullable=True)
    element = relationship("Element", back_populates="properties")


class Quantity(Base):
    __tablename__ = "quantities"
    id = Column(Integer, primary_key=True, index=True)
    element_id = Column(Integer, ForeignKey("elements.element_id", ondelete="CASCADE"), nullable=False, index=True)
    qto = Column(String(256), nullable=True, index=True)
    name = Column(String(256), nullable=False, index=True)
    value_num = Column(Float, nullable=True)
    unit = Column(String(64), nullable=True)
    element = relationship("Element", back_populates="quantities")


class Relation(Base):
    __tablename__ = "relations"
    id = Column(Integer, primary_key=True, index=True)
    model_id = Column(Integer, ForeignKey("models.model_id", ondelete="CASCADE"), nullable=False, index=True)
    from_guid = Column(String(64), nullable=False, index=True)
    rel_type = Column(String(128), nullable=False, index=True)
    to_guid = Column(String(64), nullable=False, index=True)


class Metric(Base):
    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True, index=True)
    element_id = Column(Integer, ForeignKey("elements.element_id", ondelete="CASCADE"), nullable=False, index=True)
    area = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    bbox_min = Column(JSONB, nullable=True)
    bbox_max = Column(JSONB, nullable=True)
    source = Column(String(32), nullable=True)
    element = relationship("Element", back_populates="metrics")
