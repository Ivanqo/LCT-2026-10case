from __future__ import annotations

import json
import base64
import hashlib
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from ..db.models import (
    AttributeObservation,
    AuditLog,
    CanonicalEntity,
    ChangeEvent,
    ChangeIssue,
    DisputeLog,
    DocumentVersion,
    EvidenceGroup,
    EvidenceFragment,
    LogicalRuleRecord,
    MLRetrainingLog,
    MonitoringMetric,
    InspectionProcess,
    MatrixVersion,
    NormativeBaseEntry,
    Param,
    Protocol,
    EntityAlias,
    EntityObservation,
    ReviewDecision,
    User,
    UploadJob,
)
from ..config import settings
from ..db.session import get_db
from ..domain.change_engine import ChangeEngine
from ..domain.ifc_observation_sync import sync_ifc_observations
from ..domain import protocol_body_cache
from ..domain.portrait_builder import PortraitBuilder, stage_sort_key
from ..domain.source_fragment_sync import sync_exported_source_fragments
from ..domain.stage_from_name import SOURCE_LABELS as STAGE_SOURCE_LABELS
from ..domain.synthetic_dataset import ensure_synthetic_case10_dataset
from ..domain.official_dataset import (
    MATRIX_VERSION_OFFICIAL,
    import_official_dataset,
    find_dataset_paths,
)
from ..domain.v3_pipeline import (
    DECISION_TO_STATUS,
    FINDING_STATUSES,
    MATRIX_VERSION_DEMO,
    add_audit,
    impact_scope_for_documents,
    create_process,
    current_applicable_documents,
    document_completeness,
    ensure_default_matrix,
    ensure_demo_matrix,
    evidence_group_to_dict,
    finalize_protocol,
    get_or_create_open_process,
    import_matrix_params,
    latest_protocol,
    process_to_dict,
    protocol_pdf_bytes,
    protocol_to_dict,
    record_inspector_decision,
    run_process,
    unfinalize_protocol,
    upload_scenario,
)
from ..domain.v3_jobs import enqueue_process_job, publish_job_message
from ..domain.training_release import build_training_release, training_log_to_dict
from ..domain.iais_rin_sync import fetch_documents_from_iais_rin, retry_pending_iais_rin_syncs, sync_protocol_to_iais_rin, sync_state_of
from ..domain.rejection_feedback import suggested_fix_for_reason, verdict_for_inspector_decision
from evaluation.exporter import protocol_to_evaluation_predictions, protocol_to_submission, validate_submission_basic, validate_submission_schema
from evaluation.errors import LeakageGuardError
from .auth import get_current_user, require_admin, require_ml_engineer, require_supervisor
from .utils import get_project_for_org
from ..domain.dataset_sources import render_evidence_page
from ..domain import inspector_card
from ..domain import inspector_dashboard
from ..domain import ml_weekly_report
from ..domain import protocol_export
from ..domain import inspector_workbench as workbench
from ..services.simple_documents import safe_filename

router = APIRouter(tags=["case10"])


STAGE_LABELS = {
    "project": "П",
    "working": "Р",
    "as_built": "И",
    "ifc": "IFC",
    "unknown": "Не указано",
}


class ReviewDecisionIn(BaseModel):
    decision: str = Field(pattern="^(Confirmed|Rejected|Needs Review|New)$")
    comment: str | None = Field(default=None, max_length=5000)


class InspectionProcessIn(BaseModel):
    object_id: str | None = Field(default=None, max_length=64)
    run_immediately: bool = True
    affected_param_codes: list[str] = Field(default_factory=list)
    matrix_version: str | None = Field(default=None, max_length=64)


class MatrixParamIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    section: str | None = Field(default=None, max_length=255)
    parameter_name: str = Field(min_length=1, max_length=512)
    unit: str | None = Field(default=None, max_length=64)
    source_pd: bool = True
    source_rd: bool = True
    source_id: bool = True
    trigger_logic: str | None = None
    review_priority: str = Field(default="MEDIUM", pattern="^(HIGH|MEDIUM|LOW)$")
    sp_reference: str | None = None
    gost_reference: str | None = None
    fz_reference: str | None = None
    other_normative: str | None = None
    data_type: str = "string"
    min_value: float | None = None
    max_value: float | None = None
    regex_pattern: str | None = None
    is_active: bool = True


class MatrixImportIn(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    source_filename: str | None = Field(default=None, max_length=512)
    params: list[MatrixParamIn]


class EvidenceDecisionIn(BaseModel):
    decision: str = Field(pattern="^(Confirm|Reject|Clarification Required)$")
    reason_code: str | None = Field(default=None, max_length=64)
    comment: str | None = Field(default=None, max_length=5000)
    # S5: what the workbench measured for this decision (conscious actions and ms from opening the candidate);
    # written to the audit log as DECISION_UI_METRICS, never used to decide anything.
    ui_metrics: dict[str, Any] | None = None


class BulkDecisionIn(BaseModel):
    evidence_group_ids: list[int] = Field(min_length=1, max_length=200)
    decision: str = Field(pattern="^(Confirm|Reject|Clarification Required)$")
    reason_code: str | None = Field(default=None, max_length=64)
    comment: str = Field(min_length=1, max_length=5000)
    confirm_bulk_reject: bool = False


class PartDecisionItemIn(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    decision: str = Field(pattern="^(Confirm|Reject|Clarification Required)$")
    comment: str | None = Field(default=None, max_length=2000)


class PartialDecisionIn(BaseModel):
    parts: list[PartDecisionItemIn] = Field(min_length=1, max_length=100)
    comment: str | None = Field(default=None, max_length=2000)


class HypothesisReviewIn(BaseModel):
    status: str = Field(pattern="^(CONFIRMED|REJECTED|CLARIFICATION_REQUIRED)$")
    comment: str = Field(min_length=1, max_length=2000)


class HypothesisConvertIn(BaseModel):
    comment: str = Field(min_length=1, max_length=2000)


class FragmentAddIn(BaseModel):
    document_version_id: int
    page: int = Field(ge=1)
    bbox_norm: list[float] | None = None
    extracted_value: str | None = Field(default=None, max_length=2000)
    role: str | None = Field(default="context", max_length=32)
    note: str | None = Field(default=None, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)


class FragmentRefineIn(BaseModel):
    page: int | None = Field(default=None, ge=1)
    bbox_norm: list[float] | None = None
    extracted_value: str | None = Field(default=None, max_length=2000)
    role: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)


class FragmentStatusIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class RevisionChoiceIn(BaseModel):
    scope_key: str = Field(min_length=1, max_length=255)
    document_version_id: int
    justification: str = Field(min_length=1, max_length=2000)


class UnfinalizeIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class DisputeLogIn(BaseModel):
    inspector_comment: str = Field(min_length=1, max_length=5000)
    ai_comment: str | None = Field(default=None, max_length=5000)
    reason_code: str | None = Field(default="OTHER", max_length=64)


class LogicalRuleUpdateIn(BaseModel):
    is_active: bool | None = None
    rule_name: str | None = Field(default=None, max_length=255)
    condition: str | None = None
    expected: str | None = None
    normative_base: str | None = None


class NormativeBaseIn(BaseModel):
    document_name: str = Field(min_length=1, max_length=512)
    document_number: str | None = Field(default=None, max_length=128)
    section: str | None = Field(default=None, max_length=255)
    parameter_name: str | None = Field(default=None, max_length=255)
    min_value: float | None = None
    max_value: float | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    is_active: bool = True


class NormativeBaseUpdateIn(BaseModel):
    document_name: str | None = Field(default=None, max_length=512)
    document_number: str | None = Field(default=None, max_length=128)
    section: str | None = Field(default=None, max_length=255)
    parameter_name: str | None = Field(default=None, max_length=255)
    min_value: float | None = None
    max_value: float | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    is_active: bool | None = None


class OfficialDatasetImportIn(BaseModel):
    object_ids: list[str] = Field(min_length=1)
    include_pages: bool = True
    run_processes: bool = False


class TrainingReleaseIn(BaseModel):
    dataset_version: str = Field(default="case10-inspector-gold-draft-v1", min_length=1, max_length=128)
    min_confirmed_violations: int = Field(default=100, ge=1, le=100000)
    candidate_metrics: dict[str, Any] = Field(default_factory=dict)
    baseline_metrics: dict[str, Any] = Field(default_factory=dict)


@router.post("/case10/projects/{project_id}/synthetic-dataset")
def seed_synthetic_dataset(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    result = ensure_synthetic_case10_dataset(db, project_id=project_id, organization_id=organization_id)
    ensure_demo_matrix(db, organization_id=organization_id, project_id=project_id)
    process = get_or_create_open_process(db, project_id=project_id, organization_id=organization_id, matrix_version=MATRIX_VERSION_DEMO)
    db.commit()
    process = run_process(db, process_id=str(process.id), user_id=int(user.id))
    result["process_id"] = process.id
    result["process_status"] = process.status
    return result


@router.get("/case10/dashboard")
def case10_dashboard(
    project_id: int | None = Query(default=None),
    section: str | None = Query(default=None, max_length=255),
    status: str | None = Query(default=None, description="comma-separated colours: RED,YELLOW,GREEN,GRAY"),
    date_from: str | None = Query(default=None, description="YYYY-MM-DD, by the date the check was created"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD, inclusive"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """TZ module 7: objects/checks with a colour indication (red/yellow/green/gray). The rule is `inspector_dashboard.classify`
    and is returned as `rules`; the colour is derived from the findings, never stored."""
    organization_id = _require_org(user)
    colors = {item.strip().upper() for item in (status or "").split(",") if item.strip()}
    unknown = colors - set(inspector_dashboard.COLORS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown status filter: {', '.join(sorted(unknown))}")
    try:
        return inspector_dashboard.dashboard(db, organization_id=organization_id, project_id=project_id, section=section or None,
                                             colors=colors or None, date_from=date_from, date_to=date_to, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD") from exc


@router.get("/case10/overview")
def case10_overview(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    project = get_project_for_org(db, project_id, organization_id)

    doc_rows = (
        db.query(DocumentVersion.document_stage, func.count(DocumentVersion.id))
        .filter(DocumentVersion.project_id == project_id, DocumentVersion.organization_id == organization_id)
        .group_by(DocumentVersion.document_stage)
        .all()
    )
    docs_by_stage = {stage or "unknown": count for stage, count in doc_rows}

    entity_count = (
        db.query(func.count(CanonicalEntity.id))
        .filter(CanonicalEntity.project_id == project_id, CanonicalEntity.organization_id == organization_id)
        .scalar()
        or 0
    )
    issue_rows = (
        db.query(ChangeIssue.severity, ChangeIssue.status, func.count(ChangeIssue.id))
        .join(ChangeEvent, ChangeIssue.change_event_id == ChangeEvent.id)
        .join(CanonicalEntity, ChangeEvent.canonical_entity_id == CanonicalEntity.id)
        .filter(CanonicalEntity.project_id == project_id, CanonicalEntity.organization_id == organization_id)
        .group_by(ChangeIssue.severity, ChangeIssue.status)
        .all()
    )
    issue_counts: dict[str, int] = {"CRITICAL": 0, "WARNING": 0, "NEEDS_REVIEW": 0, "LOW": 0}
    status_counts: dict[str, int] = {"New": 0, "Confirmed": 0, "Rejected": 0, "Needs Review": 0}
    total_changes = 0
    for severity, status, count in issue_rows:
        issue_counts[severity or "NEEDS_REVIEW"] = issue_counts.get(severity or "NEEDS_REVIEW", 0) + int(count)
        status_counts[status or "New"] = status_counts.get(status or "New", 0) + int(count)
        total_changes += int(count)

    process = (
        db.query(InspectionProcess)
        .filter(InspectionProcess.project_id == project_id, InspectionProcess.organization_id == organization_id)
        .order_by(InspectionProcess.created_at.desc(), InspectionProcess.id.desc())
        .first()
    )
    finding_rows = []
    if process:
        finding_rows = (
            db.query(EvidenceGroup.finding_status, func.count(EvidenceGroup.id))
            .filter(EvidenceGroup.process_id == process.id)
            .group_by(EvidenceGroup.finding_status)
            .all()
        )
    finding_counts = {status: 0 for status in sorted(FINDING_STATUSES)}
    for status, count in finding_rows:
        finding_counts[str(status)] = int(count)

    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "description": project.description,
        },
        "documents": {
            "by_stage": _stage_cards(docs_by_stage),
            "total": sum(int(v) for v in docs_by_stage.values()),
        },
        "objects": {
            "total": int(entity_count),
        },
        "changes": {
            "total": total_changes,
            "by_severity": issue_counts,
            "by_status": status_counts,
        },
        "inspection": process_to_dict(db, process) if process else None,
        "findings": finding_counts,
        "pipeline": [
            "UPLOAD ПД/РД/ИД",
            "DOCUMENT REGISTRY",
            "PARAMS / MATRIX",
            "EVIDENCE GROUPS",
            "CANDIDATE / TECH STATUSES",
            "INSPECTOR DECISION",
            "VERSIONED PROTOCOL",
            "GOLD DRAFT",
        ],
    }


@router.get("/case10/documents")
def case10_documents(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    rows = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.project_id == project_id, DocumentVersion.organization_id == organization_id)
        .order_by(DocumentVersion.document_stage.asc(), DocumentVersion.created_at.desc(), DocumentVersion.id.desc())
        .all()
    )
    current = {stage: int(doc.id) for stage, doc in current_applicable_documents(rows).items()}
    groups: dict[str, list[dict[str, Any]]] = {}
    for doc in rows:
        stage = doc.doc_stage or doc.document_stage or "unknown"
        manifest_row = (doc.dataset_metadata or {}).get("document_manifest") if isinstance(doc.dataset_metadata, dict) else None
        manifest_row = manifest_row if isinstance(manifest_row, dict) else {}
        stage_source = str(manifest_row.get("stage_source") or "") or None
        groups.setdefault(stage, []).append(
            {
                "id": doc.id,
                "object_id": doc.object_id,
                "file_id": doc.dataset_file_id,
                "filename": doc.filename,
                "stage": stage,
                "doc_stage": doc.doc_stage,
                "dataset_stage": doc.dataset_stage,
                "dataset_split": doc.dataset_split,
                "dataset_section": doc.dataset_section,
                "stage_label": STAGE_LABELS.get(stage, stage),
                "stage_source": stage_source,
                "stage_source_label": STAGE_SOURCE_LABELS.get(stage_source) if stage_source and stage != "unknown" else None,
                "stage_warning": manifest_row.get("stage_warning"),
                "discipline": doc.discipline,
                "document_code": doc.document_code,
                "version": doc.version,
                "revision": doc.revision,
                "approval_status": doc.approval_status,
                "approval_date": doc.approval_date,
                "source_type": doc.source_type,
                "source_document_id": doc.source_document_id,
                "content_hash": doc.content_hash,
                "file_hash": doc.file_hash or doc.content_hash,
                "file_path": doc.file_path,
                "predecessor_id": doc.predecessor_id,
                "successor_id": doc.successor_id,
                "is_current_approved": current.get(stage) == int(doc.id),
                "created_at": doc.created_at,
                "uploaded_at": doc.uploaded_at,
            }
        )
    return {
        "groups": [
            {
                "stage": stage,
                "stage_label": STAGE_LABELS.get(stage, stage),
                "documents": groups[stage],
            }
            for stage in sorted(groups.keys(), key=stage_sort_key)
        ]
    }


@router.post("/case10/projects/{project_id}/processes")
def create_case10_process(
    project_id: int,
    payload: InspectionProcessIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    payload = payload or InspectionProcessIn()
    process = create_process(
        db,
        project_id=project_id,
        organization_id=organization_id,
        object_id=payload.object_id,
        affected_param_codes=payload.affected_param_codes,
        matrix_version=payload.matrix_version,
    )
    job = None
    if payload.run_immediately:
        job = enqueue_process_job(
            db,
            process,
            user_id=int(user.id),
            affected_param_codes=payload.affected_param_codes,
            reason="api_start",
        )
    db.commit()
    if job:
        publish_job_message(job)
    db.refresh(process)
    return process_to_dict(db, process)


@router.post("/case10/processes/{process_id}/run")
def run_case10_process(
    process_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    process = db.get(InspectionProcess, str(process_id))
    if not process or int(process.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Inspection process not found")
    get_project_for_org(db, int(process.project_id), organization_id)
    job = enqueue_process_job(db, process, user_id=int(user.id), reason="api_run")
    db.commit()
    publish_job_message(job)
    db.refresh(process)
    return process_to_dict(db, process)


@router.get("/case10/processes")
def list_case10_processes(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    rows = (
        db.query(InspectionProcess)
        .filter(InspectionProcess.project_id == project_id, InspectionProcess.organization_id == organization_id)
        .order_by(InspectionProcess.created_at.desc(), InspectionProcess.id.desc())
        .all()
    )
    return [process_to_dict(db, row) for row in rows]


@router.get("/case10/processes/{process_id}/status")
def case10_process_status(
    process_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    process = db.get(InspectionProcess, str(process_id))
    if not process or int(process.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Inspection process not found")
    # A status poll only needs process/protocol identity, not the full findings
    # payload (can exceed 1MB and dominates this endpoint's latency under polling).
    return process_to_dict(db, process, include_protocol_payload=False)


@router.get("/case10/matrix")
def case10_matrix(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    matrix = ensure_default_matrix(db, organization_id=organization_id, project_id=project_id)
    db.commit()
    rows = (
        db.query(Param)
        .join(MatrixVersion, Param.matrix_version_id == MatrixVersion.id)
        .filter(
            Param.organization_id == organization_id,
            Param.project_id == project_id,
            MatrixVersion.version == matrix.version,
        )
        .order_by(Param.code.asc())
        .all()
    )
    return {
        "matrix": {
            "id": int(matrix.id),
            "version": matrix.version,
            "status": matrix.status,
            "source_filename": matrix.source_filename,
            "source_hash": matrix.source_hash,
            "params_count": len(rows),
            "note": "Official CASE10 132 params are default." if matrix.version == MATRIX_VERSION_OFFICIAL else "Demo fixture fallback; official catalog was not found.",
        },
        "params": [_param_item(row) for row in rows],
    }


@router.post("/case10/matrix/import")
def import_case10_matrix(
    payload: MatrixImportIn,
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    matrix = import_matrix_params(
        db,
        organization_id=organization_id,
        project_id=project_id,
        version=payload.version,
        params=[item.model_dump() for item in payload.params],
        source_filename=payload.source_filename,
    )
    db.commit()
    return {"id": matrix.id, "version": matrix.version, "params_count": len(matrix.params)}


@router.post("/case10/projects/{project_id}/official-dataset/import")
def import_case10_official_dataset(
    project_id: int,
    payload: OfficialDatasetImportIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    summary = import_official_dataset(
        db,
        project_id=project_id,
        organization_id=organization_id,
        object_ids=payload.object_ids,
        include_pages=payload.include_pages,
    )
    processes = []
    jobs = []
    if payload.run_processes:
        for object_id in summary.get("objects", []):
            process = create_process(
                db,
                project_id=project_id,
                organization_id=organization_id,
                object_id=str(object_id),
                matrix_version=MATRIX_VERSION_OFFICIAL,
            )
            db.flush()
            jobs.append(enqueue_process_job(db, process, user_id=int(user.id), reason="official_dataset_import"))
            processes.append(process_to_dict(db, process))
    db.commit()
    for job in jobs:
        publish_job_message(job)
    return {**summary, "processes": processes}


@router.get("/case10/evidence-groups")
def case10_evidence_groups(
    project_id: int = Query(...),
    process_id: str | None = Query(default=None),
    include_fragments: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    query = (
        db.query(EvidenceGroup)
        .options(
            selectinload(EvidenceGroup.param),
            selectinload(EvidenceGroup.canonical_entity),
            selectinload(EvidenceGroup.decisions),
            selectinload(EvidenceGroup.process),
        )
        .filter(EvidenceGroup.project_id == project_id, EvidenceGroup.organization_id == organization_id)
    )
    if include_fragments:
        query = query.options(
            selectinload(EvidenceGroup.fragments).selectinload(EvidenceFragment.document_version),
            selectinload(EvidenceGroup.fragments).selectinload(EvidenceFragment.source_fragment),
        )
    if process_id:
        query = query.filter(EvidenceGroup.process_id == process_id)
    rows = query.order_by(EvidenceGroup.created_at.desc(), EvidenceGroup.id.desc()).all()
    return [
        evidence_group_to_dict(db, row, include_fragments=include_fragments, include_inspector_evidence=False)
        for row in rows
    ]


@router.get("/case10/evidence-groups/{evidence_group_id}")
def case10_evidence_group(
    evidence_group_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    group = db.get(EvidenceGroup, int(evidence_group_id))
    if not group or int(group.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Evidence group not found")
    return evidence_group_to_dict(db, group, include_fragments=True)


@router.get("/case10/evidence-fragments/{fragment_id}/page.png")
def case10_evidence_page(fragment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    fragment = db.get(EvidenceFragment, fragment_id)
    if not fragment or fragment.evidence_group.organization_id != _require_org(user):
        raise HTTPException(status_code=404, detail="Evidence fragment not found")
    try:
        data = render_evidence_page(fragment.document_version, fragment)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.post("/case10/evidence-groups/{evidence_group_id}/decisions")
def create_evidence_decision(
    evidence_group_id: int,
    payload: EvidenceDecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    group = db.get(EvidenceGroup, int(evidence_group_id))
    if not group or int(group.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Evidence group not found")
    decision = record_inspector_decision(
        db,
        evidence_group_id=int(group.id),
        decision=payload.decision,
        reason_code=payload.reason_code,
        comment=payload.comment,
        user_id=int(user.id),
    )
    if payload.ui_metrics:
        workbench.record_decision_ui_metrics(
            db,
            db.get(InspectionProcess, str(group.process_id)),
            evidence_group_id=int(group.id),
            metrics={**payload.ui_metrics, "decision": payload.decision},
            user_id=int(user.id),
        )
    return {
        "id": int(decision.id),
        "evidence_group_id": int(decision.evidence_group_id),
        "decision": decision.decision,
        "finding_status": DECISION_TO_STATUS[decision.decision],
        "reason_code": decision.reason_code,
        "comment": decision.comment,
        "user_id": decision.user_id,
        "created_at": decision.created_at,
        "evidence_group": evidence_group_to_dict(db, db.get(EvidenceGroup, int(group.id)), include_fragments=True),
    }


@router.post("/case10/evidence-groups/{evidence_group_id}/disputes")
def create_dispute_log(
    evidence_group_id: int,
    payload: DisputeLogIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """ТЗ-10 `Dispute_Log`: open a dispute against a finding. Creation only --
    there is no resolution workflow/UI in this codebase yet (see
    `DisputeLog` model docstring)."""
    organization_id = _require_org(user)
    group = db.get(EvidenceGroup, int(evidence_group_id))
    if not group or int(group.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Evidence group not found")
    ai_verdict, rule_comment = verdict_for_inspector_decision(group.finding_status, None)
    row = DisputeLog(
        evidence_group_id=int(group.id),
        process_id=str(group.process_id),
        project_id=int(group.project_id),
        organization_id=int(group.organization_id),
        inspector_comment=payload.inspector_comment,
        ai_comment=payload.ai_comment or rule_comment,
        reason_code=str(payload.reason_code or "OTHER").strip().upper(),
        ai_verdict=ai_verdict,
        suggested_fix=suggested_fix_for_reason(payload.reason_code),
        opened_by_user_id=int(user.id),
    )
    db.add(row)
    db.flush()
    add_audit(
        db,
        action="DISPUTE_OPENED",
        user_id=int(user.id),
        process=db.get(InspectionProcess, str(group.process_id)),
        object_id=group.object_id,
        details={"evidence_group_id": int(group.id), "dispute_log_id": int(row.id)},
    )
    db.commit()
    db.refresh(row)
    return {
        "id": int(row.id),
        "evidence_group_id": int(row.evidence_group_id),
        "process_id": row.process_id,
        "inspector_comment": row.inspector_comment,
        "ai_comment": row.ai_comment,
        "reason_code": row.reason_code,
        "ai_verdict": row.ai_verdict,
        "suggested_fix": row.suggested_fix,
        "resolution_status": row.resolution_status,
        "opened_by_user_id": row.opened_by_user_id,
        "created_at": row.created_at,
    }


# ---------------------------------------------------------------------------------------------------------------
# S5: inspector workbench (expert session §13-17, §19, §35). Logic in domain/inspector_workbench.py.
# ---------------------------------------------------------------------------------------------------------------
def _org_process(db: Session, process_id: str, user: User) -> InspectionProcess:
    process = db.get(InspectionProcess, str(process_id))
    if not process or int(process.organization_id) != _require_org(user):
        raise HTTPException(status_code=404, detail="Inspection process not found")
    return process


def _org_group(db: Session, evidence_group_id: int, user: User) -> EvidenceGroup:
    group = db.get(EvidenceGroup, int(evidence_group_id))
    if not group or int(group.organization_id) != _require_org(user):
        raise HTTPException(status_code=404, detail="Evidence group not found")
    return group


def _org_document(db: Session, document_version_id: int, user: User) -> DocumentVersion:
    document = db.get(DocumentVersion, int(document_version_id))
    if not document or int(document.organization_id) != _require_org(user):
        raise HTTPException(status_code=404, detail="Document not found")
    return document


def _document_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc) or "Original document is not available")
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/case10/processes/{process_id}/workbench")
def case10_workbench(process_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The candidate queue of the single candidate window: one light row per evidence group."""
    return inspector_card.workbench_summary_full(db, _org_process(db, process_id, user))


@router.get("/case10/evidence-groups/{evidence_group_id}/workbench")
def case10_evidence_group_workbench(evidence_group_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The §14 evidence card + ПД/РД/ИД panels (effective evidence, page-normalized boxes) + decision/edit history."""
    return inspector_card.workbench_group_full(db, _org_group(db, evidence_group_id, user))


@router.post("/case10/evidence-groups/{evidence_group_id}/partial-decision")
def case10_partial_decision(evidence_group_id: int, payload: PartialDecisionIn, db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """Final C1 ("Разделить на части"): per-part decisions on a compound finding; once every part has one, the overall
    decision is recorded through the normal path."""
    group = _org_group(db, evidence_group_id, user)
    result = inspector_card.record_part_decisions(db, group, items=[item.model_dump() for item in payload.parts],
                                                  comment=payload.comment, user_id=int(user.id))
    return {**result, "evidence_group": inspector_card.workbench_group_full(db, _org_group(db, evidence_group_id, user))}


@router.post("/case10/evidence-groups/{evidence_group_id}/hypothesis-review")
def case10_hypothesis_review(evidence_group_id: int, payload: HypothesisReviewIn, db: Session = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """Final C1: review status of a free-search hypothesis (TZ 9.5), rationale mandatory, history append-only."""
    group = _org_group(db, evidence_group_id, user)
    result = inspector_card.record_hypothesis_review(db, group, status=payload.status, comment=payload.comment, user_id=int(user.id))
    return {**result, "evidence_group": inspector_card.workbench_group_full(db, _org_group(db, evidence_group_id, user))}


@router.post("/case10/evidence-groups/{evidence_group_id}/hypothesis-to-candidate")
def case10_hypothesis_to_candidate(evidence_group_id: int, payload: HypothesisConvertIn, db: Session = Depends(get_db),
                                   user: User = Depends(get_current_user)):
    """TZ 9.5: a hypothesis becomes a CANDIDATE only on the inspector's action and only with concrete sources and boxes."""
    group = _org_group(db, evidence_group_id, user)
    result = inspector_card.convert_hypothesis_to_candidate(db, group, comment=payload.comment, user_id=int(user.id))
    return {**result, "evidence_group": inspector_card.workbench_group_full(db, _org_group(db, evidence_group_id, user))}


@router.get("/case10/evidence-groups/{evidence_group_id}/protocol-entry")
def case10_protocol_entry(evidence_group_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The record of one finding as it goes into the protocol (sources, values, status, decision, rationale)."""
    return inspector_card.protocol_entry(db, _org_group(db, evidence_group_id, user))


@router.get("/case10/processes/{process_id}/progress")
def case10_progress(process_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Documents/pages processed, skips with reasons, found/reviewed candidates, incomplete-coverage flag."""
    return inspector_card.progress_panel(db, _org_process(db, process_id, user))


@router.get("/case10/evidence-groups/{evidence_group_id}/edits")
def case10_evidence_group_edits(evidence_group_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return workbench.group_edit_history(db, _org_group(db, evidence_group_id, user))


@router.post("/case10/evidence-groups/{evidence_group_id}/fragments")
def case10_add_fragment(evidence_group_id: int, payload: FragmentAddIn, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    group = _org_group(db, evidence_group_id, user)
    edit = workbench.add_fragment(db, group, document_version_id=payload.document_version_id, page=payload.page,
                                  bbox_norm=payload.bbox_norm, extracted_value=payload.extracted_value, role=payload.role,
                                  note=payload.note, reason=payload.reason, user_id=int(user.id))
    return {"edit": workbench.edit_to_dict(edit), "evidence_group": inspector_card.workbench_group_full(db, group)}


@router.post("/case10/evidence-groups/{evidence_group_id}/fragments/{fragment_key}/refine")
def case10_refine_fragment(evidence_group_id: int, fragment_key: str, payload: FragmentRefineIn,
                           db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    group = _org_group(db, evidence_group_id, user)
    changes = payload.model_dump(exclude_unset=True)
    changes.pop("reason", None)
    edit = workbench.refine_fragment(db, group, fragment_key, changes=changes, reason=payload.reason, user_id=int(user.id))
    return {"edit": workbench.edit_to_dict(edit), "evidence_group": inspector_card.workbench_group_full(db, group)}


@router.post("/case10/evidence-groups/{evidence_group_id}/fragments/{fragment_key}/remove")
def case10_remove_fragment(evidence_group_id: int, fragment_key: str, payload: FragmentStatusIn,
                           db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    group = _org_group(db, evidence_group_id, user)
    edit = workbench.set_fragment_status(db, group, fragment_key, remove=True, reason=payload.reason, user_id=int(user.id))
    return {"edit": workbench.edit_to_dict(edit), "evidence_group": inspector_card.workbench_group_full(db, group)}


@router.post("/case10/evidence-groups/{evidence_group_id}/fragments/{fragment_key}/restore")
def case10_restore_fragment(evidence_group_id: int, fragment_key: str, payload: FragmentStatusIn,
                            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    group = _org_group(db, evidence_group_id, user)
    edit = workbench.set_fragment_status(db, group, fragment_key, remove=False, reason=payload.reason, user_id=int(user.id))
    return {"edit": workbench.edit_to_dict(edit), "evidence_group": inspector_card.workbench_group_full(db, group)}


@router.post("/case10/evidence-groups/bulk-decisions")
def case10_bulk_decisions(payload: BulkDecisionIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """§17: same-type candidates only, a shared comment, explicit confirmation for a bulk rejection."""
    return workbench.bulk_decide(db, group_ids=payload.evidence_group_ids, organization_id=_require_org(user),
                                 decision=payload.decision, reason_code=payload.reason_code, comment=payload.comment,
                                 confirm_bulk_reject=payload.confirm_bulk_reject, user_id=int(user.id))


@router.get("/case10/processes/{process_id}/revisions")
def case10_revision_scopes(process_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return workbench.revision_scopes(db, _org_process(db, process_id, user))


@router.post("/case10/processes/{process_id}/revision-choices")
def case10_choose_revision(process_id: str, payload: RevisionChoiceIn, db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    process = _org_process(db, process_id, user)
    result = workbench.choose_revision(db, process, scope_key=payload.scope_key, document_version_id=payload.document_version_id,
                                       justification=payload.justification, user_id=int(user.id))
    return {**result, "revisions": workbench.revision_scopes(db, process)}


@router.get("/case10/processes/{process_id}/completeness")
def case10_completeness(process_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    process = _org_process(db, process_id, user)
    return {**workbench.completeness_panel(db, process), "process_upload_status": process.completeness_status or {}}


@router.post("/case10/processes/{process_id}/verification/open")
def case10_verification_open(process_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The protocol was opened in the workbench: starts the §35 verification clock (once per verification cycle)."""
    return workbench.open_verification(db, _org_process(db, process_id, user), user_id=int(user.id))


@router.get("/case10/processes/{process_id}/verification/timing")
def case10_verification_timing(process_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return workbench.verification_timing(db, _org_process(db, process_id, user))


@router.get("/case10/document-versions/{document_version_id}/pages/{page}/geometry")
def case10_page_geometry(document_version_id: int, page: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    document = _org_document(db, document_version_id, user)
    try:
        return {"document_version_id": int(document.id), **workbench.page_geometry(document, page)}
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise _document_error(exc) from exc


@router.get("/case10/document-versions/{document_version_id}/pages/{page}.png")
def case10_page_png(document_version_id: int, page: int, max_side: int = Query(1800, ge=600, le=3200),
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """A page of the original as rendered for the side-by-side panels, without any highlight (the client draws the
    overlays from bbox_norm in the same visible frame)."""
    document = _org_document(db, document_version_id, user)
    try:
        data = workbench.render_page_png(document, page, max_side=max_side)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise _document_error(exc) from exc
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@router.get("/case10/document-versions/{document_version_id}/pages/{page}.pdf")
def case10_page_pdf(document_version_id: int, page: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Go to source: the original page cut out of the original PDF (vector content and text layer intact)."""
    document = _org_document(db, document_version_id, user)
    try:
        data = workbench.extract_page_pdf(document, page)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise _document_error(exc) from exc
    return Response(data, media_type="application/pdf",
                    headers={"Cache-Control": "private, max-age=600",
                             "Content-Disposition": f'inline; filename="doc{int(document.id)}_p{int(page)}.pdf"'})


def _compact_json_response(data: Any) -> Response:
    """The body FastAPI would send for `data`, without `jsonable_encoder`: that recursive pass over a 2.6 MB protocol
    payload cost about two thirds of the request (the payload is plain JSON from the database; only the few datetime
    fields need it, and `default=` gives them exactly its encoding)."""
    body = json.dumps(data, default=jsonable_encoder, ensure_ascii=False, allow_nan=False, indent=None,
                      separators=(",", ":")).encode("utf-8")
    return Response(content=body, media_type="application/json")


# ---- sheet comparison ("Сравнение листов"): a viewing aid, behind CASE10_SHEET_COMPARE_ENABLED (default off) ----
def _require_sheet_compare() -> None:
    from ..domain.sheet_compare import config as sheet_config, raster as sheet_raster

    if not sheet_config.sheet_compare_enabled():
        raise HTTPException(status_code=404, detail="Sheet comparison is switched off (CASE10_SHEET_COMPARE_ENABLED)")
    if not sheet_raster.available():
        raise HTTPException(status_code=503, detail="Sheet comparison needs OpenCV, which is not installed")


@router.get("/case10/sheet-compare/status")
def case10_sheet_compare_status(user: User = Depends(get_current_user)):
    from ..domain.sheet_compare import service as sheet_service

    return sheet_service.status()


@router.get("/case10/processes/{process_id}/sheet-pairs")
def case10_sheet_pairs(process_id: str, left_document_id: int | None = Query(default=None), right_document_id: int | None = Query(default=None),
                       kind: str = Query(default="all", pattern="^(all|stage|revision)$"), min_confidence: float | None = Query(default=None, ge=0, le=1),
                       limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Matched sheet pairs PD <-> RD (same discipline) and RD <-> RD (revisions of one series), with the pair confidence and its basis."""
    _require_sheet_compare()
    from ..domain.comparison_gate import GateContext
    from ..domain.sheet_compare import service as sheet_service

    from ..domain.v3_pipeline import _project_documents

    process = _org_process(db, process_id, user)
    documents = _project_documents(db, process)
    kinds =("stage", "revision") if kind == "all" else (kind,)
    return sheet_service.sheet_pairs(documents, GateContext(documents), left_id=left_document_id, right_id=right_document_id, kinds=kinds,
                                     min_confidence=min_confidence, limit=limit)


def _sheet_compare_args(db: Session, user: User, left_document_id: int, right_document_id: int):
    return _org_document(db, left_document_id, user), _org_document(db, right_document_id, user)


@router.get("/case10/sheet-compare")
def case10_sheet_compare(left_document_id: int = Query(...), left_page: int = Query(..., ge=1), right_document_id: int = Query(...),
                         right_page: int = Query(..., ge=1), same_series: bool = Query(default=False),
                         db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """One sheet pair: pairing evidence (confidence), picture similarity, difference regions with boxes in PDF points of each sheet."""
    _require_sheet_compare()
    from ..domain.sheet_compare import service as sheet_service

    left, right = _sheet_compare_args(db, user, left_document_id, right_document_id)
    try:
        result, _ = sheet_service.compare_sheets(left, left_page, right, right_page, same_series=same_series)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise _document_error(exc) from exc
    return result


@router.get("/case10/sheet-compare/overlay.png")
def case10_sheet_compare_overlay(left_document_id: int = Query(...), left_page: int = Query(..., ge=1), right_document_id: int = Query(...),
                                 right_page: int = Query(..., ge=1), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The difference overlay: strokes on both sheets grey, only on the left red, only on the right blue-green."""
    _require_sheet_compare()
    from ..domain.sheet_compare import service as sheet_service

    left, right = _sheet_compare_args(db, user, left_document_id, right_document_id)
    try:
        result, overlay = sheet_service.compare_sheets(left, left_page, right, right_page, want_overlay=True)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise _document_error(exc) from exc
    if overlay is None:
        raise HTTPException(status_code=404, detail=f"No overlay: {result['picture'].get('reason') or 'not comparable'}")
    return Response(overlay, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@router.get("/case10/protocols/current")
def case10_current_protocol(
    project_id: int = Query(...),
    process_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    key = protocol_body_cache.protocol_identity(db, project_id=project_id, organization_id=organization_id, process_id=process_id)
    if key is None:
        return None
    body = protocol_body_cache.get(key)
    if body is None:
        protocol = latest_protocol(db, project_id=project_id, organization_id=organization_id, process_id=process_id)
        if not protocol:
            return None
        body = _compact_json_response(protocol_to_dict(protocol)).body
        protocol_body_cache.put(key, body)
    return Response(content=body, media_type="application/json")


@router.get("/case10/protocols/{protocol_id}/json")
def case10_protocol_json(
    protocol_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol or int(protocol.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Protocol not found")
    return protocol_to_dict(protocol)


@router.get("/case10/protocols/{protocol_id}/evaluation-predictions")
def case10_protocol_evaluation_predictions(
    protocol_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol or int(protocol.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Protocol not found")
    try:
        return protocol_to_evaluation_predictions(protocol_to_dict(protocol))
    except LeakageGuardError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/case10/processes/{process_id}/protocols")
def case10_protocol_history(process_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    process = db.get(InspectionProcess, process_id)
    if not process or process.organization_id != _require_org(user):
        raise HTTPException(status_code=404, detail="Inspection process not found")
    rows = db.query(Protocol).filter(Protocol.process_id == process_id).order_by(Protocol.version).all()
    return [protocol_to_dict(row) for row in rows]


@router.get("/case10/processes/{process_id}/audit")
def case10_process_audit(process_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    process = db.get(InspectionProcess, process_id)
    if not process or process.organization_id != _require_org(user):
        raise HTTPException(status_code=404, detail="Inspection process not found")
    rows = db.query(AuditLog).filter(AuditLog.process_id == process_id).order_by(AuditLog.id).all()
    return [{"id": row.id, "action": row.action, "user_id": row.user_id, "timestamp": row.timestamp, "details": row.details} for row in rows]


@router.get("/case10/protocols/{protocol_id}/submission")
def case10_protocol_submission(
    protocol_id: int,
    include_suspicions: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol or int(protocol.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Protocol not found")
    try:
        submission = protocol_to_submission(protocol_to_dict(protocol), include_suspicions=include_suspicions)
    except LeakageGuardError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    schema = find_dataset_paths().get("submission_schema")
    if not schema:
        raise HTTPException(status_code=503, detail="Official submission schema is unavailable")
    return {"submission": submission, "schema": schema.name, "validation_errors": validate_submission_schema(submission, schema)}


@router.get("/case10/protocols/{protocol_id}/pdf")
def case10_protocol_pdf(
    protocol_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol or int(protocol.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Protocol not found")
    data = protocol_pdf_bytes(protocol)
    return Response(content=data, media_type="application/pdf")


def _protocol_for_export(db: Session, user: User, protocol_id: int) -> dict[str, Any]:
    organization_id = _require_org(user)
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol or int(protocol.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Protocol not found")
    return protocol_to_dict(protocol)


@router.get("/case10/protocols/{protocol_id}/docx")
def case10_protocol_docx(protocol_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Annex 2 as a Word document (TZ module 7); built from the same protocol payload as the PDF and the JSON."""
    data = protocol_export.render_docx(_protocol_for_export(db, user, protocol_id))
    return Response(content=data, media_type=protocol_export.DOCX_MEDIA_TYPE,
                    headers={"Content-Disposition": f'attachment; filename="protocol_{int(protocol_id)}.docx"'})


@router.get("/case10/protocols/{protocol_id}/xml")
def case10_protocol_xml(protocol_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Annex 2 as XML (TZ module 7): the same model as the DOCX, machine-readable."""
    data = protocol_export.render_xml(_protocol_for_export(db, user, protocol_id))
    return Response(content=data, media_type=protocol_export.XML_MEDIA_TYPE,
                    headers={"Content-Disposition": f'attachment; filename="protocol_{int(protocol_id)}.xml"'})


@router.post("/case10/protocols/{protocol_id}/finalize")
async def finalize_case10_protocol(
    protocol_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol or int(protocol.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Protocol not found")
    finalized = finalize_protocol(db, protocol_id=int(protocol.id), user_id=int(user.id))
    # S5 / expert session 35: "from opening the protocol to finalization" -> VERIFICATION_TIME_MEASURED in the audit log.
    workbench.record_verification_time(
        db, db.get(InspectionProcess, str(finalized.process_id)), protocol_id=int(finalized.id), user_id=int(user.id)
    )
    # ТЗ 9.6: send the finalized protocol to ИАИС РИН, on the same request/DB
    # session that finalized it (deliberately not a fire-and-forget background
    # task: that would need its own DB session, which in a test harness that
    # overrides `get_db` with an isolated session would silently talk to the
    # wrong database). `sync_protocol_to_iais_rin` never raises on a
    # retryable/rejected send -- the outcome lands on the protocol's
    # `iais_rin_sync` state, not on this response, and is retried later by
    # `retry_pending_iais_rin_syncs`. With no target configured (the
    # hackathon default) this returns immediately.
    await sync_protocol_to_iais_rin(db, finalized)
    return protocol_to_dict(finalized)


@router.post("/case10/protocols/{protocol_id}/unfinalize")
def unfinalize_case10_protocol(
    protocol_id: int,
    payload: UnfinalizeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
):
    organization_id = _require_org(user)
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol or int(protocol.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Protocol not found")
    updated = unfinalize_protocol(db, protocol_id=int(protocol.id), user_id=int(user.id), reason=payload.reason)
    return protocol_to_dict(updated)


@router.get("/case10/protocols/{protocol_id}/iais-rin-sync")
def case10_iais_rin_sync_status(protocol_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    organization_id = _require_org(user)
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol or int(protocol.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Protocol not found")
    return sync_state_of(protocol)


@router.post("/case10/protocols/{protocol_id}/iais-rin-sync/retry")
async def case10_iais_rin_sync_retry(protocol_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Manual re-trigger of the ТЗ 9.6 ИАИС РИН sync (e.g. for an inspector
    to retry sooner than the next scheduled `next_retry_at`, or after fixing
    the target's configuration). Raises 409 unless the protocol is
    PROTOCOL_FINALIZED, same gate as the automatic sync on finalize."""
    organization_id = _require_org(user)
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol or int(protocol.organization_id) != organization_id:
        raise HTTPException(status_code=404, detail="Protocol not found")
    return await sync_protocol_to_iais_rin(db, protocol)


@router.post("/case10/iais-rin-sync/sweep")
async def case10_iais_rin_sync_sweep(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Retry every PENDING_SYNC protocol whose scheduled `next_retry_at` has
    passed (ТЗ 9.6: 1/5/15 min then hourly). Intended to be invoked on a
    cadence by an external scheduler/cron -- this codebase has no dedicated
    background timer process for it (see `iais_rin_sync.py` module docstring)."""
    results = await retry_pending_iais_rin_syncs(db)
    return {"retried": len(results), "results": results}


@router.post("/case10/processes/{process_id}/iais-rin/pull-files")
async def case10_iais_rin_pull_files(
    process_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pull a mock/adapter document bundle into an open process (emulation, no real IAИС contour)."""
    if not settings.IAIS_RIN_PULL_ENABLED:
        raise HTTPException(status_code=503, detail="ИАИС РиН file pull is disabled")
    process = _org_process(db, process_id, user)
    if str(process.status or "") == "FINALIZED":
        raise HTTPException(status_code=409, detail="Finalized protocol cannot receive documents")
    try:
        bundle = await fetch_documents_from_iais_rin(str(process.id))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ИАИС РиН file pull failed: {type(exc).__name__}: {exc}") from exc

    remote_docs = bundle.get("documents") or []
    if not remote_docs or len(remote_docs) > 500:
        raise HTTPException(status_code=422, detail="ИАИС РиН bundle must contain between 1 and 500 documents")

    # Re-read under a row lock after the network wait: a protocol finalized while the client was fetching may not
    # receive the returned files.
    process = db.query(InspectionProcess).filter(
        InspectionProcess.id == str(process_id), InspectionProcess.organization_id == int(user.organization_id or 0),
    ).with_for_update().first()
    if process is None:
        raise HTTPException(status_code=404, detail="Inspection process not found")
    if str(process.status or "") == "FINALIZED":
        raise HTTPException(status_code=409, detail="Finalized protocol cannot receive documents")

    from .. import batch_package as bp

    stage_codes = {"PD": "PD", "RD": "RD", "ID": "ID"}
    total_size = 0
    staged: list[tuple[dict[str, Any], bytes, str, str, str]] = []
    used_names: set[tuple[str, str]] = set()
    object_id = str(process.object_id or "").strip() or None
    for item in remote_docs:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="Each IAИС РиН document must be an object")
        raw_stage = str(item.get("doc_stage") or item.get("stage") or "").strip().upper()
        stage = stage_codes.get(raw_stage)
        raw_name = PurePosixPath(str(item.get("filename") or "").replace("\\", "/")).name
        filename = safe_filename(raw_name)
        if stage is None or not filename or Path(filename).suffix.lower() != ".pdf":
            raise HTTPException(status_code=422, detail="Each pulled file needs stage PD/RD/ID and a PDF filename")
        if (stage, filename.casefold()) in used_names:
            raise HTTPException(status_code=422, detail="ИАИС РиН bundle contains duplicate filenames within a stage")
        used_names.add((stage, filename.casefold()))
        try:
            content = base64.b64decode(str(item.get("content_base64") or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid base64 content for {filename}") from exc
        if not content:
            raise HTTPException(status_code=422, detail=f"Empty file in IAИС РиН bundle: {filename}")
        total_size += len(content)
        if total_size > settings.MAX_UPLOAD_PACKAGE_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="ИАИС РиН bundle exceeds the configured package size limit")
        digest = hashlib.sha256(content).hexdigest()
        expected_digest = str(item.get("sha256") or "").strip().lower()
        if expected_digest and expected_digest != digest:
            raise HTTPException(status_code=422, detail=f"SHA-256 mismatch for {filename}")
        candidate_object_id = str(item.get("object_id") or object_id or "").strip()
        if object_id and candidate_object_id and candidate_object_id != object_id:
            raise HTTPException(status_code=422, detail="ИАИС РиН bundle contains more than one object_id")
        object_id = object_id or candidate_object_id or f"RIN-P{int(process.project_id)}-{hashlib.sha256(str(process.id).encode()).hexdigest()[:12]}"
        staged.append((item, content, stage, filename, digest))

    remote_registry = bundle.get("registry")
    registry_rows = []
    if remote_registry is not None:
        if not isinstance(remote_registry, list) or not remote_registry or len(remote_registry) > 2000:
            raise HTTPException(status_code=422, detail="ИАИС РиН registry must be a non-empty array with at most 2000 rows")
        for row in remote_registry:
            if not isinstance(row, dict):
                raise HTTPException(status_code=422, detail="Each IAИС РиН registry record must be an object")
            stage = stage_codes.get(str(row.get("doc_stage") or row.get("stage") or "").strip().upper())
            raw_name = PurePosixPath(str(row.get("file_name") or row.get("filename") or "").replace("\\", "/")).name
            filename = safe_filename(raw_name)
            if stage is None or not filename or Path(filename).suffix.lower() != ".pdf":
                raise HTTPException(status_code=422, detail="Each IAИС РиН registry row needs stage PD/RD/ID and a PDF filename")
            registry_rows.append({
                "file_id": str(row.get("file_id") or "").strip() or None,
                "object_id": object_id,
                "file_name": f"files/{stage}/{filename}",
                "doc_stage": stage,
                "sha256": str(row.get("sha256") or "").strip().lower() or None,
                "approval_status": str(row.get("approval_status") or "UNKNOWN"),
                "document_code": row.get("document_code"),
                "revision": row.get("revision"),
                "approval_date": row.get("approval_date"),
            })
    else:
        for item, _content, stage, filename, digest in staged:
            registry_rows.append({
                "file_id": str(item.get("file_id") or f"RIN-{digest[:16]}"),
                "object_id": object_id,
                "file_name": f"files/{stage}/{filename}",
                "doc_stage": stage,
                "sha256": digest,
                "approval_status": str(item.get("approval_status") or "UNKNOWN"),
                "document_code": item.get("document_code"),
                "revision": item.get("revision"),
            })
    package_digest = hashlib.sha256(json.dumps(registry_rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    process_digest = hashlib.sha256(str(process.id).encode("utf-8")).hexdigest()[:16]
    package_relative = Path("_iais_rin") / str(int(process.project_id)) / process_digest / package_digest
    package_root = settings.BATCH_PACKAGES_ROOT.resolve()
    package_dir = (package_root / package_relative).resolve()
    if not package_dir.is_relative_to(package_root):
        raise HTTPException(status_code=422, detail="Invalid IAИС РиН package path")
    originals_root = os.environ.get("CASE10_ORIGINALS_ROOT", "").strip()
    if originals_root and Path(originals_root).resolve() != package_root:
        raise HTTPException(status_code=409, detail="CASE10_ORIGINALS_ROOT must match CASE10_BATCH_PACKAGES_ROOT for IAИС pulls")
    os.environ.setdefault("CASE10_ORIGINALS_ROOT", str(package_root))

    package_files = []
    for item, content, stage, filename, digest in staged:
        relative = Path("files") / stage / filename
        stored_path = package_dir / relative
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        stored_path.write_bytes(content)
        package_files.append(bp.PackageFile(relative.as_posix(), len(content), digest, None))
    registry_digest = hashlib.sha256(json.dumps(registry_rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    try:
        plan = bp.build_plan(
            package_files, registry_rows, package_name=f"iais-rin-{process.id}", object_id=object_id,
            registry_sha256=registry_digest, originals_prefix=package_relative.as_posix(),
        )
    except bp.PackageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    manifest_path = settings.DATA_DIR / "iais_rin_manifests" / f"{process_digest}-{package_digest}.jsonl"
    bp.import_plan(db, plan, project_id=int(process.project_id), organization_id=int(process.organization_id), manifest_path=manifest_path)
    process.object_id = plan.object_id
    imported_ids = {str(row["file_id"]) for row in plan.rows}
    documents = db.query(DocumentVersion).filter(
        DocumentVersion.project_id == int(process.project_id),
        DocumentVersion.organization_id == int(process.organization_id),
        DocumentVersion.object_id == plan.object_id,
        DocumentVersion.dataset_file_id.in_(sorted(imported_ids)),
    ).order_by(DocumentVersion.id.asc()).all()
    process.completeness_status = document_completeness(documents)
    process.upload_scenario = upload_scenario(process.completeness_status)
    db.add(process)
    upload_job = UploadJob(
        project_id=int(process.project_id), organization_id=int(process.organization_id), user_id=int(user.id),
        filename="ИАИС РиН: пакет документов", file_type="pdf", temp_path=str(package_dir), file_size=total_size,
        process_id=str(process.id), source_type="case10_dataset", status="ready", stage="Файлы получены из mock-контура ИАИС РиН",
        progress=100, detail=f"Импортировано документов: {len(documents)}; эмуляция внешнего контура",
    )
    db.add(upload_job)
    db.flush()
    from .routes_upload import _recompute_case10_for_uploaded_documents
    case10_job = _recompute_case10_for_uploaded_documents(db, upload_job, documents)
    add_audit(db, action="IAIS_RIN_FILES_PULLED", user_id=int(user.id), process=process,
              details={"upload_job_id": int(upload_job.id), "documents": len(documents), "sha256": [item[4] for item in staged], "emulation": True})
    db.commit()
    if case10_job is not None:
        published = publish_job_message(case10_job)
        if settings.OFFLINE_DELIVERY and not published:
            from .routes_upload import _execute_case10_job_inline
            background_tasks.add_task(_execute_case10_job_inline, dict(case10_job.payload_json or {}))
    return {
        "process_id": str(process.id), "upload_job_id": int(upload_job.id),
        "case10_job_id": str(case10_job.id) if case10_job else None,
        "documents_pulled": len(documents), "completeness": process.completeness_status,
        "emulation": True, "registry_status": plan.report.get("registry_status"),
    }


@router.post("/case10/projects/{project_id}/training-release")
def create_case10_training_release(
    project_id: int,
    payload: TrainingReleaseIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_ml_engineer),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    payload = payload or TrainingReleaseIn()
    return build_training_release(
        db,
        project_id=project_id,
        organization_id=organization_id,
        dataset_version=payload.dataset_version,
        min_confirmed_violations=payload.min_confirmed_violations,
        candidate_metrics=payload.candidate_metrics,
        baseline_metrics=payload.baseline_metrics,
        user_id=int(user.id),
    )


@router.get("/case10/projects/{project_id}/ml-retraining-log")
def list_case10_ml_retraining_log(
    project_id: int,
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_ml_engineer),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    rows = (
        db.query(MLRetrainingLog)
        .filter(MLRetrainingLog.project_id == int(project_id), MLRetrainingLog.organization_id == organization_id)
        .order_by(MLRetrainingLog.created_at.desc(), MLRetrainingLog.id.desc())
        .limit(int(limit))
        .all()
    )
    return [training_log_to_dict(row) for row in rows]


@router.get("/case10/ml/reports/weekly")
def case10_ml_weekly_report(
    end: str | None = Query(default=None, description="last day of the 7-day window, YYYY-MM-DD (default: today, UTC)"),
    project_id: int | None = Query(default=None),
    format: str = Query(default="json", pattern="^(json|html)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_ml_engineer),
):
    """TZ module 10: weekly report for ML engineers (inspector decisions by reason, section and method, change to the previous
    week, recommendations, retraining log). Honest about small samples: see `data_sufficiency`."""
    organization_id = _require_org(user)
    if project_id:
        get_project_for_org(db, project_id, organization_id)
    try:
        report = ml_weekly_report.build_weekly_report(db, organization_id=organization_id, project_id=project_id, end=end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="end must be YYYY-MM-DD") from exc
    if format == "html":
        return Response(content=ml_weekly_report.render_html(report), media_type="text/html; charset=utf-8")
    return report


@router.get("/case10/ml/reports/weekly/saved")
def case10_list_saved_weekly_reports(
    project_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_ml_engineer),
):
    """List persisted scheduler snapshots for the current organization."""
    organization_id = _require_org(user)
    if project_id:
        # Project ownership is validated with the same check as report generation.
        get_project_for_org(db, project_id, organization_id)
    return ml_weekly_report.list_saved_reports(organization_id=organization_id, project_id=project_id, limit=limit)


def _logical_rule_to_dict(row: LogicalRuleRecord) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "rule_id": row.rule_id,
        "rule_name": row.rule_name,
        "condition": row.condition,
        "expected": row.expected,
        "normative_base": row.normative_base,
        "criticality": row.criticality,
        "confidence": row.confidence,
        "is_active": bool(row.is_active),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/case10/logical-rules")
def list_logical_rules(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """ТЗ-10 `Logical_Rules` catalog. Read-only for any authenticated user;
    only `is_active` is meant to be admin-edited at runtime (see
    `LogicalRuleRecord` docstring)."""
    rows = db.query(LogicalRuleRecord).order_by(LogicalRuleRecord.rule_id.asc()).all()
    return [_logical_rule_to_dict(row) for row in rows]


@router.patch("/case10/logical-rules/{rule_id}")
def update_logical_rule(
    rule_id: str,
    payload: LogicalRuleUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    row = db.query(LogicalRuleRecord).filter(LogicalRuleRecord.rule_id == rule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Logical rule not found")
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.rule_name is not None:
        row.rule_name = payload.rule_name
    if payload.condition is not None:
        row.condition = payload.condition
    if payload.expected is not None:
        row.expected = payload.expected
    if payload.normative_base is not None:
        row.normative_base = payload.normative_base
    db.add(row)
    add_audit(db, action="LOGICAL_RULE_UPDATED", user_id=int(user.id), details={"rule_id": rule_id, **payload.model_dump(exclude_unset=True)})
    db.commit()
    db.refresh(row)
    return _logical_rule_to_dict(row)


def _normative_base_to_dict(row: NormativeBaseEntry) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "document_name": row.document_name,
        "document_number": row.document_number,
        "section": row.section,
        "parameter_name": row.parameter_name,
        "min_value": row.min_value,
        "max_value": row.max_value,
        "effective_from": row.effective_from,
        "effective_to": row.effective_to,
        "is_active": bool(row.is_active),
        "created_by_user_id": row.created_by_user_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/case10/normative-base")
def list_normative_base(
    is_active: bool | None = Query(default=None),
    parameter_name: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(NormativeBaseEntry)
    if is_active is not None:
        query = query.filter(NormativeBaseEntry.is_active == is_active)
    if parameter_name:
        query = query.filter(NormativeBaseEntry.parameter_name == parameter_name)
    rows = query.order_by(NormativeBaseEntry.id.desc()).all()
    return [_normative_base_to_dict(row) for row in rows]


@router.post("/case10/normative-base")
def create_normative_base(payload: NormativeBaseIn, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """ТЗ-10 `Normative_Base` / ТЗ module 8: admin registry of normative
    documents and their threshold values."""
    row = NormativeBaseEntry(
        document_name=payload.document_name,
        document_number=payload.document_number,
        section=payload.section,
        parameter_name=payload.parameter_name,
        min_value=payload.min_value,
        max_value=payload.max_value,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        is_active=payload.is_active,
        created_by_user_id=int(user.id),
    )
    db.add(row)
    db.flush()
    add_audit(db, action="NORMATIVE_BASE_CREATED", user_id=int(user.id), details={"normative_base_id": int(row.id)})
    db.commit()
    db.refresh(row)
    return _normative_base_to_dict(row)


@router.patch("/case10/normative-base/{entry_id}")
def update_normative_base(
    entry_id: int,
    payload: NormativeBaseUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    row = db.get(NormativeBaseEntry, int(entry_id))
    if not row:
        raise HTTPException(status_code=404, detail="Normative base entry not found")
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field_name, value)
    db.add(row)
    # mode="json" here (not above): AuditLog.details is a JSON column with no
    # datetime-aware encoder configured, so effective_from/effective_to must
    # be ISO strings by the time they land in `details`, while the ORM
    # assignment above needs the real `datetime` objects.
    add_audit(db, action="NORMATIVE_BASE_UPDATED", user_id=int(user.id), details={"normative_base_id": entry_id, **payload.model_dump(exclude_unset=True, mode="json")})
    db.commit()
    db.refresh(row)
    return _normative_base_to_dict(row)


@router.delete("/case10/normative-base/{entry_id}")
def delete_normative_base(entry_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    row = db.get(NormativeBaseEntry, int(entry_id))
    if not row:
        raise HTTPException(status_code=404, detail="Normative base entry not found")
    db.delete(row)
    add_audit(db, action="NORMATIVE_BASE_DELETED", user_id=int(user.id), details={"normative_base_id": entry_id})
    db.commit()
    return {"deleted": True, "id": entry_id}


@router.get("/case10/monitoring-metrics")
def list_monitoring_metrics(
    metric_name: str | None = Query(default=None),
    service_name: str | None = Query(default=None),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(require_ml_engineer),
):
    """ТЗ-10 `Monitoring_Metrics`: durable counterpart to the in-memory
    Prometheus counters at `/metrics`. Written internally (see
    `record_monitoring_metric` in `iais_rin_sync.py`), not user-created."""
    query = db.query(MonitoringMetric)
    if metric_name:
        query = query.filter(MonitoringMetric.metric_name == metric_name)
    if service_name:
        query = query.filter(MonitoringMetric.service_name == service_name)
    rows = query.order_by(MonitoringMetric.timestamp.desc(), MonitoringMetric.id.desc()).limit(int(limit)).all()
    return [
        {
            "id": int(row.id),
            "metric_name": row.metric_name,
            "value": row.value,
            "timestamp": row.timestamp,
            "service_name": row.service_name,
            "tags": row.tags,
        }
        for row in rows
    ]


@router.post("/case10/document-versions/{document_version_id}/sync-source-fragments")
async def sync_document_source_fragments(
    document_version_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    doc_version = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.id == int(document_version_id), DocumentVersion.organization_id == organization_id)
        .first()
    )
    if not doc_version:
        raise HTTPException(status_code=404, detail="DocumentVersion not found")
    get_project_for_org(db, int(doc_version.project_id), organization_id)
    if doc_version.source_type != "rag" or doc_version.source_document_id is None:
        raise HTTPException(status_code=400, detail="Source fragment sync is currently supported for RAG DocumentVersion only")

    from ..clients.rag_client import RagClient

    exported = await RagClient(timeout=60.0, retries=1).export_source_fragments(
        document_id=int(doc_version.source_document_id),
        organization_id=organization_id,
    )
    result = sync_exported_source_fragments(db, doc_version, exported)
    process = get_or_create_open_process(db, project_id=int(doc_version.project_id), organization_id=organization_id)
    impact_scope = impact_scope_for_documents(db, process, [doc_version])
    affected = impact_scope["param_codes"]
    if affected:
        process.affected_param_codes = affected
        db.add(process)
        db.flush()
        job = enqueue_process_job(
            db,
            process,
            user_id=int(user.id),
            affected_param_codes=affected,
            reason="rag_source_fragment_sync",
            impact_scope=impact_scope,
        )
        db.commit()
        publish_job_message(job)
    else:
        db.commit()
    return {
        "document_version_id": int(doc_version.id),
        "source_type": doc_version.source_type,
        "source_document_id": int(doc_version.source_document_id),
        "exported": len(exported),
        "process_id": str(process.id),
        "affected_param_codes": affected,
        **result,
    }


@router.post("/case10/document-versions/{document_version_id}/sync-ifc-observations")
async def sync_document_ifc_observations(
    document_version_id: int,
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    doc_version = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.id == int(document_version_id), DocumentVersion.organization_id == organization_id)
        .first()
    )
    if not doc_version:
        raise HTTPException(status_code=404, detail="DocumentVersion not found")
    get_project_for_org(db, int(doc_version.project_id), organization_id)
    if doc_version.source_type != "ifc" or doc_version.source_document_id is None:
        raise HTTPException(status_code=400, detail="IFC observation sync is supported for IFC DocumentVersion only")

    from ..clients.ifc_client import IfcClient

    exported = await IfcClient(timeout=120.0).export_observations(
        model_id=int(doc_version.source_document_id),
        organization_id=organization_id,
        limit=int(limit),
    )
    result = sync_ifc_observations(db, doc_version, exported)
    process = get_or_create_open_process(db, project_id=int(doc_version.project_id), organization_id=organization_id)
    impact_scope = impact_scope_for_documents(db, process, [doc_version])
    affected = impact_scope["param_codes"]
    if affected:
        process.affected_param_codes = affected
        db.add(process)
        db.flush()
        job = enqueue_process_job(
            db,
            process,
            user_id=int(user.id),
            affected_param_codes=affected,
            reason="ifc_observation_sync",
            impact_scope=impact_scope,
        )
        db.commit()
        publish_job_message(job)
    else:
        db.commit()
    return {
        "document_version_id": int(doc_version.id),
        "source_type": doc_version.source_type,
        "source_document_id": int(doc_version.source_document_id),
        "exported": len(exported),
        "process_id": str(process.id),
        "affected_param_codes": affected,
        **result,
    }


@router.get("/entities")
def list_entities(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    entities = (
        db.query(CanonicalEntity)
        .filter(CanonicalEntity.project_id == project_id, CanonicalEntity.organization_id == organization_id)
        .order_by(CanonicalEntity.entity_type.asc(), CanonicalEntity.id.asc())
        .all()
    )
    return [_entity_registry_item(db, entity) for entity in entities]


@router.get("/entities/{entity_id}/portrait")
def entity_portrait(
    entity_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    return PortraitBuilder().build(db, entity_id=entity_id, organization_id=organization_id)


@router.get("/case10/changes")
def list_changes(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    rows = (
        db.query(ChangeEvent, ChangeIssue, CanonicalEntity)
        .join(CanonicalEntity, ChangeEvent.canonical_entity_id == CanonicalEntity.id)
        .outerjoin(ChangeIssue, ChangeIssue.change_event_id == ChangeEvent.id)
        .filter(CanonicalEntity.project_id == project_id, CanonicalEntity.organization_id == organization_id)
        .order_by(ChangeIssue.risk_score.desc().nullslast(), ChangeEvent.id.asc())
        .all()
    )
    return [_change_item(db, event, issue, entity) for event, issue, entity in rows]


@router.get("/case10/protocol")
def protocol_items(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    get_project_for_org(db, project_id, organization_id)
    rows = (
        db.query(ChangeIssue, ChangeEvent, CanonicalEntity)
        .join(ChangeEvent, ChangeIssue.change_event_id == ChangeEvent.id)
        .join(CanonicalEntity, ChangeEvent.canonical_entity_id == CanonicalEntity.id)
        .filter(CanonicalEntity.project_id == project_id, CanonicalEntity.organization_id == organization_id)
        .order_by(ChangeIssue.created_at.desc(), ChangeIssue.id.desc())
        .all()
    )
    return [_protocol_item(db, issue, event, entity) for issue, event, entity in rows]


@router.post("/issues/{issue_id}/decisions")
def create_review_decision(
    issue_id: int,
    payload: ReviewDecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    row = (
        db.query(ChangeIssue, ChangeEvent, CanonicalEntity)
        .join(ChangeEvent, ChangeIssue.change_event_id == ChangeEvent.id)
        .join(CanonicalEntity, ChangeEvent.canonical_entity_id == CanonicalEntity.id)
        .filter(ChangeIssue.id == int(issue_id), CanonicalEntity.organization_id == organization_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Issue not found")
    issue, event, entity = row
    issue.status = payload.decision
    decision = ReviewDecision(
        issue_id=issue.id,
        decision=payload.decision,
        comment=payload.comment,
        reviewer_id=user.id,
    )
    db.add(issue)
    db.add(decision)
    db.commit()
    db.refresh(decision)
    return {
        "id": decision.id,
        "issue_id": issue.id,
        "decision": decision.decision,
        "comment": decision.comment,
        "reviewer_id": decision.reviewer_id,
        "created_at": decision.created_at,
        "issue": _protocol_item(db, issue, event, entity),
    }


@router.post("/entities/{entity_id}/refresh-changes")
def refresh_entity_changes(
    entity_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    organization_id = _require_org(user)
    entity = (
        db.query(CanonicalEntity)
        .filter(CanonicalEntity.id == entity_id, CanonicalEntity.organization_id == organization_id)
        .first()
    )
    if not entity:
        raise HTTPException(status_code=404, detail="Canonical entity not found")
    events = ChangeEngine().refresh_entity_changes(db, entity.id)
    db.commit()
    return {"entity_id": entity.id, "change_events": len(events)}


def _require_org(user: User) -> int:
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")
    return int(user.organization_id)


def _stage_cards(counts: dict[str, int]) -> list[dict[str, Any]]:
    stages = ["project", "working", "as_built", "ifc", "unknown"]
    seen = set(stages)
    ordered = stages + [stage for stage in counts.keys() if stage not in seen]
    return [
        {"stage": stage, "label": STAGE_LABELS.get(stage, stage), "count": int(counts.get(stage, 0))}
        for stage in ordered
        if stage != "unknown" or counts.get(stage, 0)
    ]


def _entity_registry_item(db: Session, entity: CanonicalEntity) -> dict[str, Any]:
    aliases = (
        db.query(EntityAlias)
        .filter(EntityAlias.canonical_entity_id == entity.id, EntityAlias.status == "active")
        .order_by(EntityAlias.alias_type.asc(), EntityAlias.alias.asc())
        .all()
    )
    stages = (
        db.query(DocumentVersion.document_stage)
        .join(EntityObservation, EntityObservation.document_version_id == DocumentVersion.id)
        .filter(EntityObservation.canonical_entity_id == entity.id)
        .distinct()
        .all()
    )
    issue_rows = (
        db.query(ChangeIssue)
        .join(ChangeEvent, ChangeIssue.change_event_id == ChangeEvent.id)
        .filter(ChangeEvent.canonical_entity_id == entity.id)
        .all()
    )
    max_issue = max(issue_rows, key=lambda item: float(item.risk_score or 0), default=None)
    return {
        "id": entity.id,
        "canonical_name": entity.canonical_name,
        "entity_type": entity.entity_type,
        "status": entity.status,
        "aliases": [alias.alias for alias in aliases],
        "stages": [stage for (stage,) in sorted(stages, key=lambda item: stage_sort_key(item[0]))],
        "changes_count": len(issue_rows),
        "risk": {
            "score": float(max_issue.risk_score or 0) if max_issue else 0,
            "severity": max_issue.severity if max_issue else "NONE",
            "status": max_issue.status if max_issue else "New",
        },
    }


def _param_item(row: Param) -> dict[str, Any]:
    metadata = _param_metadata(row)
    return {
        "id": int(row.id),
        "matrix_version_id": int(row.matrix_version_id),
        "code": row.code,
        "matrix_code": row.matrix_code or metadata.get("matrix_code"),
        "scoring_code": row.scoring_code or metadata.get("scoring_code") or row.code,
        "aliases": row.aliases_json or metadata.get("aliases") or [value for value in (row.matrix_code, row.code) if value],
        "section": row.section,
        "parameter_name": row.parameter_name,
        "unit": row.unit,
        "source_pd": bool(row.source_pd),
        "source_rd": bool(row.source_rd),
        "source_id": bool(row.source_id),
        "source_pd_ref": metadata.get("source_pd"),
        "source_rd_ref": metadata.get("source_rd"),
        "source_id_ref": metadata.get("source_id"),
        "parameter_id": metadata.get("parameter_id"),
        "matrix_row": metadata.get("matrix_row"),
        "criticality": metadata.get("criticality"),
        "mapping_status": metadata.get("mapping_status"),
        "trigger_logic": row.trigger_logic,
        "review_priority": row.review_priority,
        "sp_reference": row.sp_reference,
        "gost_reference": row.gost_reference,
        "fz_reference": row.fz_reference,
        "other_normative": row.other_normative,
        "data_type": row.data_type,
        "min_value": row.min_value,
        "max_value": row.max_value,
        "regex_pattern": row.regex_pattern,
        "is_active": bool(row.is_active),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _param_metadata(row: Param) -> dict[str, Any]:
    try:
        if row.other_normative:
            parsed = json.loads(row.other_normative)
            return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
    return {}


def _change_item(db: Session, event: ChangeEvent, issue: ChangeIssue | None, entity: CanonicalEntity) -> dict[str, Any]:
    return {
        "id": event.id,
        "entity_id": entity.id,
        "entity_name": entity.canonical_name,
        "entity_type": entity.entity_type,
        "parameter": event.attribute,
        "change_type": event.change_type,
        "stage_values": _stage_values_for_attribute(db, entity.id, event.attribute),
        "risk": {
            "score": float(issue.risk_score or 0) if issue else 0,
            "severity": issue.severity if issue else "NONE",
            "explanation": issue.explanation if issue else None,
        },
        "confidence": event.confidence,
        "status": issue.status if issue else "New",
        "issue_id": issue.id if issue else None,
        "delta": event.delta,
    }


def _protocol_item(db: Session, issue: ChangeIssue, event: ChangeEvent, entity: CanonicalEntity) -> dict[str, Any]:
    return _change_item(db, event, issue, entity) | {
        "protocol_status": issue.status,
        "created_at": issue.created_at,
    }


def _stage_values_for_attribute(db: Session, entity_id: str, attribute: str) -> dict[str, Any]:
    rows = (
        db.query(AttributeObservation, EntityObservation, DocumentVersion)
        .join(EntityObservation, AttributeObservation.entity_observation_id == EntityObservation.id)
        .join(DocumentVersion, EntityObservation.document_version_id == DocumentVersion.id)
        .filter(EntityObservation.canonical_entity_id == entity_id, AttributeObservation.attribute_name == attribute)
        .all()
    )
    out: dict[str, Any] = {}
    for attr, _obs, doc in sorted(rows, key=lambda item: stage_sort_key(item[2].document_stage)):
        value = attr.normalized_numeric if attr.normalized_numeric is not None else attr.normalized_value or attr.raw_value
        out[doc.document_stage] = {
            "label": STAGE_LABELS.get(doc.document_stage, doc.document_stage),
            "value": value,
            "raw_value": attr.raw_value,
            "unit": attr.unit,
            "confidence": attr.confidence,
        }
    return out
