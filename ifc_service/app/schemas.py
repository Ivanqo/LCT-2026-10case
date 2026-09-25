from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
class ImportResponse(BaseModel): model_id: int
class ModelStatus(BaseModel):
    model_id: int; organization_id: Optional[int]=None; project_id: int; status: str; error: Optional[str]=None; created_at: datetime; indexed_at: Optional[datetime]=None
class ModelItem(BaseModel):
    model_id: int; organization_id: Optional[int]=None; project_id: int; original_filename: str; created_at: datetime; status: str; source_type: str="ifc"
    class Config: from_attributes = True
class DeleteModelResponse(BaseModel): ok: bool=True; model_id: int; deleted_files: int=0
class HeavyQueryRequest(BaseModel): model_id: int; query_spec: Dict[str, Any]=Field(default_factory=dict)
class HeavyQueryResponse(BaseModel): rows: List[Dict[str, Any]]
class IfcContextRequest(BaseModel):
    project_id: int; organization_id: Optional[int]=None; question: str=Field(min_length=1); max_models: int=Field(default=3, ge=1, le=10); limit: int=Field(default=15, ge=1, le=100)
class IfcContextItem(BaseModel):
    model_id: int; file_name: str; summary: str; row_data: Dict[str, Any]=Field(default_factory=dict); relevance: float=0.0
class IfcContextResponse(BaseModel):
    context_text: str; items: List[IfcContextItem]=Field(default_factory=list); used_model_ids: List[int]=Field(default_factory=list)
class IfcObservationExportItem(BaseModel):
    source_system: str = "ifc"
    external_id: str
    guid: str
    ifc_type: str
    name: Optional[str] = None
    storey_guid: Optional[str] = None
    container_guid: Optional[str] = None
    attributes: Dict[str, Any] = Field(default_factory=dict)
    quantities: Dict[str, Any] = Field(default_factory=dict)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    text: str = ""
