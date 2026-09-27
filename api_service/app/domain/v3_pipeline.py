from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import re
import uuid
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, defer

from ..db.models import (
    AttributeObservation,
    AuditLog,
    Case10ProcessJob,
    CanonicalEntity,
    ConstructionObject,
    DocumentVersion,
    EntityAlias,
    EntityObservation,
    EvidenceDecision,
    EvidenceFragment,
    EvidenceGroup,
    GoldCheckFixture,
    GoldDraftItem,
    InspectionProcess,
    MatrixVersion,
    Param,
    ProcessingCache,
    Protocol,
    RejectionLog,
    SourceFragment,
)
from .official_dataset import DATASET_VERSION_OFFICIAL, MATRIX_VERSION_OFFICIAL, dataset_stage_for_internal, ensure_official_matrix, dataset_object_registry_id
from .v3_extractors import ExtractionResult, _normalize_bbox, default_extractors
from .v3_messaging import publish_process_event, redis_get_json, redis_set_json
from .evidence_groups import compute_basis_hash, sweep_orphaned_evidence_groups, upsert_evidence_group
from .explication_compare import collect_explication_groups, explication_compare_enabled
from .live_candidate_tagger import live_tagger_coverage
from .official_evidence import create_official_evidence_groups
from .comparison_gate import GateContext
from .protocol_annex2 import annex2_payload, render_protocol_pdf
from ..request_context import get_client_ip
from evaluation.exporter import evidence_group_to_submission_check


PROCESS_PENDING = "PENDING"
PROCESS_QUEUED = "QUEUED"
PROCESS_PROCESSING = "PROCESSING"
PROCESS_PARSING = "PARSING"
PROCESS_READY = "READY"
PROCESS_FAILED = "FAILED"
PROCESS_VERIFYING = "VERIFYING"
PROCESS_COMPLETED = "COMPLETED"
PROCESS_FINALIZED = "FINALIZED"

PROCESS_STATUSES = {
    PROCESS_PENDING,
    PROCESS_QUEUED,
    PROCESS_PROCESSING,
    PROCESS_PARSING,
    PROCESS_READY,
    PROCESS_FAILED,
    PROCESS_VERIFYING,
    PROCESS_COMPLETED,
    PROCESS_FINALIZED,
}

STATUS_CANDIDATE = "CANDIDATE"
STATUS_CONFIRMED_VIOLATION = "CONFIRMED_VIOLATION"
STATUS_NEGATIVE_VERIFIED = "NEGATIVE_VERIFIED"
STATUS_MISSING_EVIDENCE = "MISSING_EVIDENCE"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_NOT_COMPARABLE = "NOT_COMPARABLE"
STATUS_CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
STATUS_SUSPICION = "SUSPICION"

FINDING_STATUSES = {
    STATUS_CANDIDATE,
    STATUS_CONFIRMED_VIOLATION,
    STATUS_NEGATIVE_VERIFIED,
    STATUS_MISSING_EVIDENCE,
    STATUS_NOT_APPLICABLE,
    STATUS_NOT_COMPARABLE,
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_SUSPICION,
}

DECISION_TO_STATUS = {
    "Confirm": STATUS_CONFIRMED_VIOLATION,
    "Reject": STATUS_NEGATIVE_VERIFIED,
    "Clarification Required": STATUS_CLARIFICATION_REQUIRED,
}

# Finding-level verification lifecycle: an inspector decision can only move a
# finding between these "decidable" statuses (freely, including re-deciding).
# MISSING_EVIDENCE / NOT_APPLICABLE / NOT_COMPARABLE / SUSPICION are technical
# outcomes of the comparison engine, not inspector verdicts, so a decision
# submitted against a finding currently in one of those statuses is rejected
# rather than silently turning a technical status into a violation.
DECIDABLE_FINDING_STATUSES = {
    STATUS_CANDIDATE,
    STATUS_CONFIRMED_VIOLATION,
    STATUS_NEGATIVE_VERIFIED,
    STATUS_CLARIFICATION_REQUIRED,
}

FINDING_DECISION_TRANSITIONS: dict[str, set[str]] = {
    current: {STATUS_CONFIRMED_VIOLATION, STATUS_NEGATIVE_VERIFIED, STATUS_CLARIFICATION_REQUIRED}
    for current in DECIDABLE_FINDING_STATUSES
}


def validate_finding_transition(current_status: str, next_status: str) -> None:
    allowed = FINDING_DECISION_TRANSITIONS.get(current_status)
    if not allowed or next_status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Finding in status {current_status} cannot receive an inspector decision "
                f"resulting in {next_status}"
            ),
        )

REVIEW_PRIORITIES = {"HIGH", "MEDIUM", "LOW"}
APPROVED_STATUSES = {"APPROVED", "SIGNED", "ISSUED_FOR_CONSTRUCTION", "УТВЕРЖДЕН", "УТВЕРЖДЕНО", "СОГЛАСОВАНО"}
DOC_STAGES = ("project", "working", "as_built")
STAGE_TO_COMPLETENESS = {
    "project": ("PD_UPLOADED", "PD_MISSING"),
    "working": ("RD_UPLOADED", "RD_MISSING"),
    "as_built": ("ID_UPLOADED", "ID_MISSING"),
}
STAGE_TO_SOURCE_FIELD = {
    "project": "source_pd",
    "working": "source_rd",
    "as_built": "source_id",
}
STAGE_SHORT = {
    "project": "ПД",
    "working": "РД",
    "as_built": "ИД",
}

MATRIX_VERSION_DEMO = "demo-fixture-v3"
DATASET_VERSION_LOCAL = "case10-local-v3"
MODEL_VERSION_RULES = "rule-extractors-v1"

logger = logging.getLogger(__name__)


DEMO_PARAMS = [
    {
        "code": "DEMO-KR-55",
        "section": "КР",
        "parameter_name": "Класс / показатель огнестойкости конструкции",
        "unit": None,
        "trigger_logic": "attribute=fire_resistance;entity_type=wall",
        "review_priority": "HIGH",
        "data_type": "string",
        "regex_pattern": r"(?:fire[_\s-]?resistance|огнестойк\w*)\s*[=:]?\s*(?P<value>EI\s*\d+)|(?P<value2>EI\s*\d+)",
        "source_pd": True,
        "source_rd": True,
        "source_id": True,
    },
    {
        "code": "DEMO-AR-01",
        "section": "АР",
        "parameter_name": "Толщина конструкции",
        "unit": "mm",
        "trigger_logic": "attribute=thickness;entity_type=wall",
        "review_priority": "HIGH",
        "data_type": "number",
        "regex_pattern": r"(?:thickness|толщина)\s*[=:]?\s*(?P<value>\d+(?:[,.]\d+)?)\s*(?P<unit>mm|мм)?",
        "source_pd": True,
        "source_rd": True,
        "source_id": True,
    },
    {
        "code": "DEMO-KR-02",
        "section": "КР",
        "parameter_name": "Материал конструкции",
        "unit": None,
        "trigger_logic": "attribute=material;entity_type=wall",
        "review_priority": "MEDIUM",
        "data_type": "string",
        "regex_pattern": r"(?:material|материал)\s*[=:]?\s*(?P<value>[A-Za-zА-Яа-яЁё0-9._-]+)",
        "source_pd": True,
        "source_rd": True,
        "source_id": True,
    },
    {
        "code": "DEMO-PZ-01",
        "section": "ПЗУ",
        "parameter_name": "Площадь застройки",
        "unit": "m2",
        "trigger_logic": "attribute=building_area",
        "review_priority": "MEDIUM",
        "data_type": "number",
        "regex_pattern": r"(?:building\s*area|площадь\s+застройки)\s*[=:]?\s*(?P<value>\d+(?:[,.]\d+)?)\s*(?P<unit>m2|м2|м²)?",
        "source_pd": True,
        "source_rd": False,
        "source_id": True,
    },
    {
        "code": "DEMO-AR-41",
        "section": "АР",
        "parameter_name": "Ширина эвакуационных дверей",
        "unit": "mm",
        "trigger_logic": "attribute=evacuation_door_width;entity_type=door",
        "review_priority": "LOW",
        "data_type": "number",
        "regex_pattern": r"(?:evacuation\s+door\s+width|ширина\s+эвакуац\w+\s+двер)\s*[=:]?\s*(?P<value>\d+(?:[,.]\d+)?)\s*(?P<unit>mm|мм)?",
        "source_pd": True,
        "source_rd": True,
        "source_id": True,
    },
]


@dataclass(slots=True)
class StageValue:
    stage: str
    document: DocumentVersion
    value: str
    normalized_value: str
    confidence: float
    extractor: str
    source_fragment: SourceFragment | None
    page: int | None
    bbox: list[float] | None
    context: str | None = None


def ensure_default_construction_object(db: Session, *, project_id: int, organization_id: int) -> ConstructionObject:
    object_id = default_object_id(project_id)
    existing = (
        db.query(ConstructionObject)
        .filter(
            ConstructionObject.id == object_id,
            ConstructionObject.project_id == int(project_id),
            ConstructionObject.organization_id == int(organization_id),
        )
        .first()
    )
    if existing:
        return existing
    obj = ConstructionObject(
        id=object_id,
        project_id=int(project_id),
        organization_id=int(organization_id),
        name=f"Construction Object #{project_id}",
    )
    db.add(obj)
    db.flush()
    return obj


def default_object_id(project_id: int) -> str:
    return f"OBJ-{int(project_id):06d}"


def ensure_demo_matrix(db: Session, *, organization_id: int, project_id: int | None = None) -> MatrixVersion:
    matrix = (
        db.query(MatrixVersion)
        .filter(
            MatrixVersion.organization_id == int(organization_id),
            MatrixVersion.project_id == project_id,
            MatrixVersion.version == MATRIX_VERSION_DEMO,
        )
        .first()
    )
    if not matrix:
        matrix = MatrixVersion(
            organization_id=int(organization_id),
            project_id=project_id,
            version=MATRIX_VERSION_DEMO,
            status="ACTIVE",
            source_filename="demo-fixture-not-official-132",
            source_hash=_hash_json(DEMO_PARAMS),
        )
        db.add(matrix)
        db.flush()

    existing_codes = {
        code
        for (code,) in db.query(Param.code)
        .filter(Param.matrix_version_id == int(matrix.id))
        .all()
    }
    for item in DEMO_PARAMS:
        if item["code"] in existing_codes:
            continue
        db.add(
            Param(
                matrix_version_id=int(matrix.id),
                project_id=project_id,
                organization_id=int(organization_id),
                code=item["code"],
                section=item.get("section"),
                parameter_name=item["parameter_name"],
                unit=item.get("unit"),
                source_pd=bool(item.get("source_pd", True)),
                source_rd=bool(item.get("source_rd", True)),
                source_id=bool(item.get("source_id", True)),
                trigger_logic=item.get("trigger_logic"),
                review_priority=item.get("review_priority", "MEDIUM"),
                data_type=item.get("data_type", "string"),
                regex_pattern=item.get("regex_pattern"),
                is_active=True,
            )
        )
    db.flush()
    return matrix


def ensure_default_matrix(db: Session, *, organization_id: int, project_id: int | None = None) -> MatrixVersion:
    try:
        return ensure_official_matrix(db, organization_id=organization_id, project_id=project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Official parameter_catalog_132.jsonl is not available")


def ensure_matrix_version(
    db: Session,
    *,
    organization_id: int,
    project_id: int | None,
    matrix_version: str | None,
) -> MatrixVersion:
    if matrix_version == MATRIX_VERSION_DEMO:
        return ensure_demo_matrix(db, organization_id=organization_id, project_id=project_id)
    if matrix_version == MATRIX_VERSION_OFFICIAL:
        return ensure_official_matrix(db, organization_id=organization_id, project_id=project_id)
    if matrix_version:
        matrix = (
            db.query(MatrixVersion)
            .filter(
                MatrixVersion.organization_id == int(organization_id),
                MatrixVersion.project_id == project_id,
                MatrixVersion.version == str(matrix_version),
            )
            .first()
        )
        if matrix:
            return matrix
    return ensure_default_matrix(db, organization_id=organization_id, project_id=project_id)


def import_matrix_params(
    db: Session,
    *,
    organization_id: int,
    project_id: int | None,
    version: str,
    params: list[dict[str, Any]],
    source_filename: str | None = None,
) -> MatrixVersion:
    clean_version = str(version or "").strip()
    if not clean_version:
        raise HTTPException(status_code=400, detail="matrix version is required")
    if not params:
        raise HTTPException(status_code=400, detail="params list is empty")

    matrix = MatrixVersion(
        organization_id=int(organization_id),
        project_id=project_id,
        version=clean_version,
        status="ACTIVE",
        source_filename=source_filename,
        source_hash=_hash_json(params),
    )
    db.add(matrix)
    db.flush()
    for item in params:
        code = str(item.get("code") or "").strip()
        name = str(item.get("parameter_name") or item.get("name") or "").strip()
        if not code or not name:
            continue
        db.add(
            Param(
                matrix_version_id=int(matrix.id),
                project_id=project_id,
                organization_id=int(organization_id),
                code=code,
                section=item.get("section"),
                parameter_name=name,
                unit=item.get("unit"),
                source_pd=bool(item.get("source_pd", True)),
                source_rd=bool(item.get("source_rd", True)),
                source_id=bool(item.get("source_id", True)),
                trigger_logic=item.get("trigger_logic"),
                review_priority=_priority(item.get("review_priority")),
                sp_reference=item.get("sp_reference"),
                gost_reference=item.get("gost_reference"),
                fz_reference=item.get("fz_reference"),
                other_normative=item.get("other_normative"),
                data_type=str(item.get("data_type") or "string"),
                min_value=_optional_float(item.get("min_value")),
                max_value=_optional_float(item.get("max_value")),
                regex_pattern=item.get("regex_pattern"),
                is_active=bool(item.get("is_active", True)),
            )
        )
    db.flush()
    return matrix


def list_active_params(db: Session, *, organization_id: int, project_id: int | None, matrix_version: str | None = None) -> list[Param]:
    if not matrix_version:
        matrix = ensure_default_matrix(db, organization_id=organization_id, project_id=project_id)
        matrix_version = matrix.version
    rows = (
        db.query(Param)
        .join(MatrixVersion, Param.matrix_version_id == MatrixVersion.id)
        .filter(
            Param.organization_id == int(organization_id),
            Param.project_id == project_id,
            MatrixVersion.version == str(matrix_version),
            Param.is_active == True,  # noqa: E712
        )
        .order_by(Param.code.asc())
        .all()
    )
    if rows:
        return rows
    if matrix_version == MATRIX_VERSION_OFFICIAL:
        ensure_official_matrix(db, organization_id=organization_id, project_id=project_id)
    elif matrix_version == MATRIX_VERSION_DEMO:
        ensure_demo_matrix(db, organization_id=organization_id, project_id=project_id)
    return (
        db.query(Param)
        .join(MatrixVersion, Param.matrix_version_id == MatrixVersion.id)
        .filter(
            Param.organization_id == int(organization_id),
            Param.project_id == project_id,
            MatrixVersion.version == str(matrix_version),
            Param.is_active == True,  # noqa: E712
        )
        .order_by(Param.code.asc())
        .all()
    )


def affected_param_codes_for_documents(db: Session, process: InspectionProcess, documents: list[DocumentVersion]) -> list[str]:
    params = list_active_params(
        db,
        organization_id=int(process.organization_id),
        project_id=int(process.project_id),
        matrix_version=str(process.matrix_version or MATRIX_VERSION_DEMO),
    )
    stages = {_doc_stage(doc) for doc in documents}
    if not stages or "unknown" in stages:
        return [str(param.code) for param in params]

    out: list[str] = []
    for param in params:
        needed = set(_param_needed_stages(param))
        if needed & stages:
            out.append(str(param.code))
    return out


def get_or_create_open_process(
    db: Session,
    *,
    project_id: int,
    organization_id: int,
    object_id: str | None = None,
    matrix_version: str | None = None,
) -> InspectionProcess:
    obj = ensure_default_construction_object(db, project_id=project_id, organization_id=organization_id)
    resolved_object_id = object_id or obj.id
    matrix = ensure_matrix_version(db, organization_id=organization_id, project_id=project_id, matrix_version=matrix_version)
    existing = (
        db.query(InspectionProcess)
        .filter(
            InspectionProcess.project_id == int(project_id),
            InspectionProcess.organization_id == int(organization_id),
            InspectionProcess.object_id == resolved_object_id,
            InspectionProcess.matrix_version == matrix.version,
            InspectionProcess.status != PROCESS_FINALIZED,
        )
        .order_by(InspectionProcess.created_at.desc(), InspectionProcess.id.desc())
        .first()
    )
    if existing:
        return existing

    process = InspectionProcess(
        id=uuid.uuid4().hex,
        project_id=int(project_id),
        organization_id=int(organization_id),
        object_id=resolved_object_id,
        status=PROCESS_PENDING,
        upload_scenario="SINGLE_ONLY",
        completeness_status={},
        matrix_version=matrix.version,
        dataset_version=DATASET_VERSION_OFFICIAL if matrix.version == MATRIX_VERSION_OFFICIAL else DATASET_VERSION_LOCAL,
        model_version=MODEL_VERSION_RULES,
    )
    db.add(process)
    db.flush()
    add_audit(db, action="PROCESS_CREATED", user_id=None, process=process, details={"status": PROCESS_PENDING})
    return process


def create_process(
    db: Session,
    *,
    project_id: int,
    organization_id: int,
    object_id: str | None = None,
    affected_param_codes: list[str] | None = None,
    matrix_version: str | None = None,
) -> InspectionProcess:
    obj = ensure_default_construction_object(db, project_id=project_id, organization_id=organization_id)
    matrix = ensure_matrix_version(db, organization_id=organization_id, project_id=project_id, matrix_version=matrix_version)
    process = InspectionProcess(
        id=uuid.uuid4().hex,
        project_id=int(project_id),
        organization_id=int(organization_id),
        object_id=object_id or obj.id,
        status=PROCESS_PENDING,
        upload_scenario="SINGLE_ONLY",
        completeness_status={},
        affected_param_codes=affected_param_codes or [],
        matrix_version=matrix.version,
        dataset_version=DATASET_VERSION_OFFICIAL if matrix.version == MATRIX_VERSION_OFFICIAL else DATASET_VERSION_LOCAL,
        model_version=MODEL_VERSION_RULES,
    )
    db.add(process)
    db.flush()
    add_audit(db, action="PROCESS_CREATED", user_id=None, process=process, details={"status": PROCESS_PENDING})
    return process


def current_document_versions(docs: Iterable[DocumentVersion]) -> list[DocumentVersion]:
    """Drop obsolete revisions from a document set before it feeds extraction.
    `_link_revision_chain` (run at upload time, when a re-uploaded filename
    matches an existing document_stage+document_code family) sets
    `successor_id` on the row it supersedes; a document whose successor is
    also present in this same set is not the current revision of its family
    and must not be used as a comparison reference. Documents with no chain
    link recorded (including every row in the pre-seeded official dataset,
    which never populates predecessor/successor) pass through unchanged."""
    docs = list(docs)
    ids = {int(doc.id) for doc in docs if doc.id is not None}
    return [doc for doc in docs if not (doc.successor_id and int(doc.successor_id) in ids)]


def impact_scope_for_documents(db: Session, process: InspectionProcess, documents: list[DocumentVersion]) -> dict[str, Any]:
    """Descriptive impact set for a batch of new/changed documents: the
    affected parameter codes plus the object/stage/discipline/document_code/
    revision/predecessor-successor context that produced that scope, for
    audit trails and job payloads."""
    return {
        "param_codes": affected_param_codes_for_documents(db, process, documents),
        "documents": [
            {
                "document_version_id": int(doc.id),
                "object_id": doc.object_id,
                "stage": _doc_stage(doc),
                "discipline": doc.discipline,
                "document_code": doc.document_code,
                "revision": doc.revision,
                "approval_status": doc.approval_status,
                "predecessor_id": doc.predecessor_id,
                "successor_id": doc.successor_id,
                "content_hash": doc.file_hash or doc.content_hash,
            }
            for doc in documents
        ],
    }


def run_process(db: Session, *, process_id: str, user_id: int | None = None, affected_param_codes: list[str] | None = None) -> InspectionProcess:
    process = _require_process(db, process_id)
    if process.status == PROCESS_FINALIZED:
        raise HTTPException(status_code=409, detail="Finalized protocol cannot be recomputed")

    try:
        _set_process_status(db, process, PROCESS_PARSING, user_id=user_id)
        docs = _project_documents(db, process)
        completeness = document_completeness(docs)
        process.completeness_status = completeness
        process.upload_scenario = upload_scenario(completeness)
        process.input_manifest_hash = input_manifest_hash(docs)
        db.add(process)
        db.flush()

        params = list_active_params(
            db,
            organization_id=int(process.organization_id),
            project_id=int(process.project_id),
            matrix_version=str(process.matrix_version or MATRIX_VERSION_DEMO),
        )
        # An explicitly passed list (including an empty one) always wins and
        # means exactly what it says; only fall back to whatever scope was
        # last queued on the process when the caller passed nothing at all.
        # `process.affected_param_codes` is reset to [] below on every
        # successful run precisely so that fallback never goes stale.
        scoped_codes = affected_param_codes if affected_param_codes is not None else (process.affected_param_codes or [])
        target_codes = {str(code) for code in scoped_codes if str(code).strip()}
        if target_codes:
            params = [param for param in params if param.code in target_codes]
        _delete_existing_gold_fixture_groups(db, process, target_codes)
        extraction_docs = current_document_versions(docs)

        cache_key = f"case10:v3:{process.input_manifest_hash}:{process.matrix_version}:{','.join(sorted(p.code for p in params))}"
        cached = redis_get_json(cache_key) or _db_cache_get(db, cache_key)
        if cached:
            add_audit(db, action="PROCESS_CACHE_HIT", user_id=user_id, process=process, details={"cache_key": cache_key})

        if process.matrix_version == MATRIX_VERSION_OFFICIAL:
            process.model_version = "official-rule-packs-v2"
            fallback_diagnostics = create_official_evidence_groups(db, process, params, extraction_docs, user_id=user_id)
            if fallback_diagnostics:
                add_audit(db, action="RULE_FALLBACK_DISCOVERY", user_id=user_id, process=process, details={"attempts": fallback_diagnostics})
            if explication_compare_enabled():
                # Phase 12 / S1: PD <-> RD room and apartment schedules (explication_compare.py), behind its flag
                explication = collect_explication_groups(db, process, params, extraction_docs, user_id=user_id)
                add_audit(db, action="EXPLICATION_COMPARE", user_id=user_id, process=process, details=explication)
        else:
            current_docs = current_applicable_documents(extraction_docs)
            entities = _entities_for_process(db, process)
            touched_keys: dict[int, set[str]] = {}
            for param in params:
                param_touched = touched_keys.setdefault(int(param.id), set())
                target_entities = _target_entities_for_param(param, entities)
                if not target_entities:
                    key, _group = _create_not_applicable_group(db, process, param, user_id=user_id)
                    param_touched.add(key)
                    continue
                for entity in target_entities:
                    key, _group = _create_evidence_group_for_param(db, process, param, entity, docs, current_docs, user_id=user_id)
                    param_touched.add(key)
            sweep_orphaned_evidence_groups(db, process, {int(p.id) for p in params}, touched_keys, user_id=user_id)

        process.status = PROCESS_READY
        # This scope has now been consumed; clear it so a later call that
        # passes no explicit affected_param_codes (a plain manual re-run)
        # falls back to a full recompute instead of silently staying scoped
        # to whatever the last incremental trigger happened to narrow it to.
        process.affected_param_codes = []
        protocol = create_protocol_version(db, process, user_id=user_id)
        _set_process_status(db, process, PROCESS_READY, user_id=user_id, details={"protocol_id": protocol.id, "version": protocol.version, "affected_param_codes": sorted(target_codes)})
        _db_cache_set(db, cache_key, {"protocol_id": protocol.id, "version": protocol.version})
        redis_set_json(cache_key, {"protocol_id": protocol.id, "version": protocol.version})
        process.error = None
        db.commit()
        return process
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        process = _require_process(db, process_id)
        process.status = PROCESS_FAILED
        process.retry_count = int(process.retry_count or 0) + 1
        process.error = f"{type(exc).__name__}: {exc}"
        db.add(process)
        add_audit(db, action="PROCESS_FAILED", user_id=user_id, process=process, details={"error": process.error})
        db.commit()
        publish_process_event({"process_id": process.id, "status": process.status, "error": process.error})
        raise


def _fragment_row_fingerprint(fragment: EvidenceFragment) -> dict[str, Any]:
    return {
        "document_version_id": fragment.document_version_id,
        "source_fragment_id": fragment.source_fragment_id,
        "page": fragment.page,
        "extracted_value": fragment.extracted_value,
        "role": fragment.role,
        "file_sha256": fragment.file_sha256,
        "revision": fragment.revision,
    }


def _apply_pending_reverification(db: Session, group: EvidenceGroup) -> bool:
    """When an inspector decides on a group flagged `needs_reverification`,
    fold the freshly recomputed values a prior run parked under
    `delta.pending_reverification` into the group's real fields, so the
    decision is recorded against what the inspector actually reviewed rather
    than leaving the flag set forever. Fragments are left as they are (a
    single-cycle lag is a documented, non-blocking limitation, see
    CASE10_IMPLEMENTATION_PROGRESS.md); the basis hash is advanced against
    those existing fragments so an unrelated future rerun does not
    immediately re-flag the same, now-acknowledged, mismatch."""
    if not group.needs_reverification:
        return False
    delta = group.delta if isinstance(group.delta, dict) else {}
    pending = delta.get("pending_reverification")
    if not isinstance(pending, dict):
        group.needs_reverification = False
        group.basis_changed_at = None
        return False
    merged_delta = dict(delta)
    merged_delta.pop("pending_reverification", None)
    group.expected_value = pending.get("expected_value")
    group.actual_value = pending.get("actual_value")
    group.delta = pending.get("delta") if pending.get("delta") is not None else merged_delta
    group.needs_reverification = False
    group.basis_changed_at = None
    existing_fragments = (
        db.query(EvidenceFragment)
        .filter(EvidenceFragment.evidence_group_id == int(group.id))
        .all()
    )
    group.evidence_basis_hash = compute_basis_hash(
        expected_value=group.expected_value,
        actual_value=group.actual_value,
        delta=group.delta,
        fragment_specs=[_fragment_row_fingerprint(fragment) for fragment in existing_fragments],
    )
    return True


def record_inspector_decision(
    db: Session,
    *,
    evidence_group_id: int,
    decision: str,
    reason_code: str | None,
    comment: str | None,
    user_id: int | None,
) -> EvidenceDecision:
    decision_label = str(decision or "").strip()
    if decision_label not in DECISION_TO_STATUS:
        raise HTTPException(status_code=400, detail="Unsupported inspector decision")
    group = db.get(EvidenceGroup, int(evidence_group_id))
    if not group:
        raise HTTPException(status_code=404, detail="Evidence group not found")
    process = _require_process(db, str(group.process_id))
    if process.status == PROCESS_FINALIZED:
        raise HTTPException(status_code=409, detail="Finalized protocol cannot be changed")
    validate_finding_transition(str(group.finding_status), DECISION_TO_STATUS[decision_label])
    if decision_label == "Reject" and not (str(reason_code or "").strip() or str(comment or "").strip()):
        raise HTTPException(status_code=422, detail="Reject requires a reason or comment")
    if decision_label == "Reject" and not str(reason_code or "").strip():
        reason_code = "OTHER"

    group.finding_status = DECISION_TO_STATUS[decision_label]
    group.updated_at = datetime.utcnow()
    reverified = _apply_pending_reverification(db, group)
    row = EvidenceDecision(
        evidence_group_id=int(group.id),
        decision=decision_label,
        reason_code=reason_code,
        comment=comment,
        user_id=user_id,
    )
    db.add(group)
    db.add(row)
    db.flush()
    _upsert_gold_draft_item(db, group, row)
    if decision_label == "Reject":
        _record_rejection_log(db, process, group, row)
    add_audit(
        db,
        action="INSPECTOR_DECISION",
        user_id=user_id,
        process=process,
        object_id=group.object_id,
        details={
            "evidence_group_id": int(group.id),
            "decision": decision_label,
            "finding_status": group.finding_status,
            "reason_code": reason_code,
            "comment": comment,
            "reverified_stale_evidence": reverified,
            "protocol_version": int(db.query(func.max(Protocol.version)).filter(Protocol.process_id == process.id).scalar() or 0) + 1,
        },
    )
    _update_process_after_decision(db, process, user_id=user_id)
    create_protocol_version(db, process, user_id=user_id)
    db.commit()
    db.refresh(row)
    return row


def _protocol_diff(previous_payload: dict[str, Any] | None, findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Human-readable diff of this protocol version against the one before
    it, at the granularity of individual findings (EvidenceGroup rows) --
    what a "previous version preserved + understandable diff" requirement
    means in practice: an inspector can see exactly which findings are new,
    which changed, and which are untouched, not just a version number bump."""
    new_by_id = {int(row["id"]): row for row in findings}
    if not previous_payload:
        return {
            "baseline": True,
            "added": sorted(new_by_id),
            "updated": [],
            "removed": [],
            "unchanged": [],
            "needs_reverification": sorted(gid for gid, row in new_by_id.items() if row.get("needs_reverification")),
        }
    old_by_id = {int(row["id"]): row for row in (previous_payload.get("findings") or [])}
    added = sorted(set(new_by_id) - set(old_by_id))
    removed = sorted(set(old_by_id) - set(new_by_id))
    common = set(new_by_id) & set(old_by_id)
    updated = sorted(
        gid for gid in common
        if any(
            new_by_id[gid].get(field) != old_by_id[gid].get(field)
            for field in ("finding_status", "needs_reverification", "expected", "actual", "delta")
        )
    )
    unchanged = sorted(common - set(updated))
    return {
        "baseline": False,
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
        "needs_reverification": sorted(gid for gid, row in new_by_id.items() if row.get("needs_reverification")),
    }


def create_protocol_version(db: Session, process: InspectionProcess, *, user_id: int | None = None) -> Protocol:
    previous = (
        db.query(Protocol)
        .filter(Protocol.process_id == process.id)
        .order_by(Protocol.version.desc())
        .first()
    )
    latest_version = int(previous.version) if previous else 0
    payload = _json_safe(build_protocol_payload(db, process))
    payload["version"] = latest_version + 1
    payload["protocol_status"] = "DRAFT"
    payload["diff"] = _protocol_diff(previous.payload_json if previous else None, payload["findings"])
    protocol = Protocol(
        process_id=str(process.id),
        project_id=int(process.project_id),
        organization_id=int(process.organization_id),
        object_id=process.object_id,
        version=latest_version + 1,
        matrix_version=str(process.matrix_version or MATRIX_VERSION_DEMO),
        dataset_version=str(process.dataset_version or DATASET_VERSION_LOCAL),
        model_version=str(process.model_version or MODEL_VERSION_RULES),
        input_manifest_hash=process.input_manifest_hash,
        status="DRAFT",
        payload_json=payload,
    )
    db.add(protocol)
    db.flush()
    add_audit(
        db,
        action="PROTOCOL_VERSION_CREATED",
        user_id=user_id,
        process=process,
        object_id=process.object_id,
        details={"protocol_id": protocol.id, "version": protocol.version, "diff": payload["diff"]},
    )
    return protocol


def finalize_protocol(db: Session, *, protocol_id: int, user_id: int | None = None) -> Protocol:
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol:
        raise HTTPException(status_code=404, detail="Protocol not found")
    process = _require_process(db, str(protocol.process_id))
    if protocol.status == "FINALIZED":
        return protocol
    if process.status == PROCESS_FINALIZED:
        raise HTTPException(status_code=409, detail="Inspection process is already finalized")
    latest = latest_protocol(db, project_id=process.project_id, organization_id=process.organization_id, process_id=process.id)
    if not latest or latest.id != protocol.id:
        raise HTTPException(status_code=409, detail="Only the latest protocol version can be finalized")
    pending = pending_candidate_count(db, process)
    if pending:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot finalize: {pending} CANDIDATE finding(s) still awaiting an inspector decision. "
                "Confirm, reject, or mark them Clarification Required first."
            ),
        )
    now = datetime.utcnow()
    protocol.status = "FINALIZED"
    protocol.finalized_at = now
    protocol.finalized_by_user_id = user_id
    process.status = PROCESS_FINALIZED
    process.finalized_at = now
    process.finalized_by_user_id = user_id
    protocol.payload_json = {**(protocol.payload_json or {}), "status": PROCESS_FINALIZED,
                             "protocol_status": "FINALIZED", "verification_status": "PROTOCOL_FINALIZED",
                             "finalized_by": user_id, "finalized_at": now.isoformat()}
    db.add(protocol)
    db.add(process)
    add_audit(
        db,
        action="PROTOCOL_FINALIZED",
        user_id=user_id,
        process=process,
        object_id=process.object_id,
        details={"protocol_id": protocol.id, "version": protocol.version, "input_manifest_hash": protocol.input_manifest_hash},
    )
    db.commit()
    db.refresh(protocol)
    publish_process_event({"process_id": process.id, "status": PROCESS_FINALIZED, "protocol_id": protocol.id})
    return protocol


def unfinalize_protocol(db: Session, *, protocol_id: int, user_id: int | None, reason: str) -> Protocol:
    reason = str(reason or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="Unfinalize requires a reason")
    protocol = db.get(Protocol, int(protocol_id))
    if not protocol:
        raise HTTPException(status_code=404, detail="Protocol not found")
    process = _require_process(db, str(protocol.process_id))
    if protocol.status != "FINALIZED" or process.status != PROCESS_FINALIZED:
        raise HTTPException(status_code=409, detail="Protocol is not finalized")

    now = datetime.utcnow()
    previous_finalized_at = protocol.finalized_at
    previous_finalized_by = protocol.finalized_by_user_id
    protocol.status = "DRAFT"
    protocol.unfinalized_at = now
    protocol.unfinalized_by_user_id = user_id
    protocol.unfinalize_reason = reason
    process.status = PROCESS_COMPLETED
    process.finalized_at = None
    process.finalized_by_user_id = None
    protocol.payload_json = {
        **(protocol.payload_json or {}),
        "status": PROCESS_COMPLETED,
        "protocol_status": "DRAFT",
        "verification_status": PROCESS_COMPLETED,
        "unfinalized_by": user_id,
        "unfinalized_at": now.isoformat(),
        "unfinalize_reason": reason,
    }
    db.add(protocol)
    db.add(process)
    add_audit(
        db,
        action="PROTOCOL_UNFINALIZED",
        user_id=user_id,
        process=process,
        object_id=process.object_id,
        details={
            "protocol_id": protocol.id,
            "version": protocol.version,
            "reason": reason,
            "previous_finalized_at": previous_finalized_at.isoformat() if previous_finalized_at else None,
            "previous_finalized_by": previous_finalized_by,
        },
    )
    db.commit()
    db.refresh(protocol)
    publish_process_event({"process_id": process.id, "status": PROCESS_COMPLETED, "protocol_id": protocol.id})
    return protocol


def latest_protocol(
    db: Session, *, project_id: int, organization_id: int, process_id: str | None = None, include_payload: bool = True
) -> Protocol | None:
    query = db.query(Protocol).filter(Protocol.project_id == int(project_id), Protocol.organization_id == int(organization_id))
    if process_id:
        query = query.filter(Protocol.process_id == process_id)
    if not include_payload:
        # payload_json holds the full findings/annex_2 payload (can exceed 1MB); a
        # status poll only needs id/version/status, so skip fetching and decoding it.
        query = query.options(defer(Protocol.payload_json))
    return query.order_by(Protocol.created_at.desc(), Protocol.version.desc(), Protocol.id.desc()).first()


def build_protocol_payload(db: Session, process: InspectionProcess) -> dict[str, Any]:
    groups = (
        db.query(EvidenceGroup)
        .filter(EvidenceGroup.process_id == process.id)
        .order_by(EvidenceGroup.review_priority.asc(), EvidenceGroup.id.asc())
        .all()
    )
    counts = Counter(str(group.finding_status or STATUS_MISSING_EVIDENCE) for group in groups)
    findings = [evidence_group_to_dict(db, group, include_fragments=True) for group in groups]
    matrix_size = len(list_active_params(db, organization_id=process.organization_id, project_id=process.project_id, matrix_version=process.matrix_version))
    obj = db.query(ConstructionObject).filter(
        ConstructionObject.organization_id == process.organization_id, ConstructionObject.project_id == process.project_id,
        ConstructionObject.id.in_((process.object_id, dataset_object_registry_id(process.object_id, process.project_id, process.organization_id))),
    ).first() if process.object_id else None
    documents = _project_documents(db, process)
    project_docs = _project_documents(db, process)
    document_analysis = GateContext(project_docs).protocol_section()
    return {
        "process_id": process.id,
        "object_id": process.object_id,
        "status": process.status,
        "upload_scenario": process.upload_scenario,
        "completeness": process.completeness_status or {},
        "document_analysis": document_analysis,
        "matrix_version": process.matrix_version,
        "dataset_version": process.dataset_version,
        "model_version": process.model_version,
        "input_manifest_hash": process.input_manifest_hash,
        # How much of the object's corpus the live candidate tagger actually
        # read ("просканировано N из M документов" per stage). Explicit so a
        # protocol produced under the tagger's deterministic caps (or with it
        # disabled) says so instead of looking exhaustive. Derived from the
        # documents' own scan markers -- no timings, so it is reproducible.
        "live_tagger_coverage": live_tagger_coverage(current_document_versions(documents)),
        "sections": {
            "upload_completeness": process.completeness_status or {},
            "comparability": _count_by(groups, "comparability_status"),
            "candidates": counts.get(STATUS_CANDIDATE, 0),
            "confirmed_violations": counts.get(STATUS_CONFIRMED_VIOLATION, 0),
            "negative_verified": counts.get(STATUS_NEGATIVE_VERIFIED, 0),
            "missing_evidence": counts.get(STATUS_MISSING_EVIDENCE, 0),
            "clarification_required": counts.get(STATUS_CLARIFICATION_REQUIRED, 0),
            "suspicions": counts.get(STATUS_SUSPICION, 0),
        },
        "findings": findings,
        "annex_2": annex2_payload(
            findings,
            documents,
            object_info={
                "object_name": obj.name if obj else process.object_id,
                "address": obj.address if obj else None,
                "supervisory_case_number": obj.permit_number if obj else None,
                "developer": obj.customer if obj else None,
                "contractor": obj.contractor if obj else None,
            },
            matrix_size=matrix_size,
        ),
    }


def process_to_dict(db: Session, process: InspectionProcess, *, include_protocol_payload: bool = True) -> dict[str, Any]:
    counts = _status_counts(db, process)
    protocol = latest_protocol(
        db,
        project_id=int(process.project_id),
        organization_id=int(process.organization_id),
        process_id=str(process.id),
        include_payload=include_protocol_payload,
    )
    latest_job = (
        db.query(Case10ProcessJob)
        .filter(Case10ProcessJob.process_id == str(process.id))
        .order_by(Case10ProcessJob.created_at.desc(), Case10ProcessJob.id.desc())
        .first()
    )
    return {
        "process_id": process.id,
        "project_id": process.project_id,
        "object_id": process.object_id,
        "status": process.status,
        "upload_scenario": process.upload_scenario,
        "completeness": process.completeness_status or {},
        "matrix_version": process.matrix_version,
        "dataset_version": process.dataset_version,
        "model_version": process.model_version,
        "input_manifest_hash": process.input_manifest_hash,
        "retry_count": process.retry_count,
        "error": process.error,
        "created_at": process.created_at,
        "updated_at": process.updated_at,
        "finalized_at": process.finalized_at,
        "finalized_by_user_id": process.finalized_by_user_id,
        "pending_candidates": counts.get(STATUS_CANDIDATE, 0),
        "job": _job_to_dict(latest_job),
        "finding_counts": counts,
        "protocol": protocol_to_dict(protocol, include_payload=include_protocol_payload) if protocol else None,
    }


def _job_to_dict(job: Case10ProcessJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "job_id": str(job.id),
        "status": job.status,
        "attempt_count": int(job.attempt_count or 0),
        "max_attempts": int(job.max_attempts or 0),
        "error": job.error,
        "retry_reason": job.retry_reason,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def evidence_group_to_dict(db: Session, group: EvidenceGroup, *, include_fragments: bool = True) -> dict[str, Any]:
    param = group.param or (db.get(Param, int(group.param_id)) if group.param_id else None)
    entity = group.canonical_entity or (db.get(CanonicalEntity, str(group.canonical_entity_id)) if group.canonical_entity_id else None)
    decisions = sorted(group.decisions, key=lambda row: row.id)
    inspector_status = {"Confirm": "CONFIRMED", "Reject": "REJECTED", "Clarification Required": "CLARIFICATION_REQUIRED"}.get(decisions[-1].decision, "PENDING") if decisions else "PENDING"
    try:
        param_metadata = json.loads(param.other_normative or "{}") if param else {}
    except (ValueError, TypeError):
        param_metadata = {}
    if not isinstance(param_metadata, dict):
        param_metadata = {}
    payload = {
        "id": int(group.id),
        "process_id": group.process_id,
        "object_id": group.object_id,
        "canonical_entity_id": group.canonical_entity_id,
        "entity_name": entity.canonical_name if entity else None,
        "param_id": group.param_id,
        "parameter": {
            "code": param.code if param else None,
            "matrix_code": getattr(param, "matrix_code", None) if param else param_metadata.get("matrix_code"),
            "scoring_code": getattr(param, "scoring_code", None) if param else param_metadata.get("scoring_code"),
            "aliases": getattr(param, "aliases_json", None) if param else param_metadata.get("aliases"),
            "section": param.section if param else None,
            "name": param.parameter_name if param else None,
            "unit": param.unit if param else None,
            "review_priority": group.review_priority,
            "criticality": param_metadata.get("criticality"),
        },
        "matrix_version": group.matrix_version,
        "model_version": group.model_version,
        "dataset_version": group.dataset_version,
        "comparison_scenario": group.comparison_scenario,
        "completeness_status": group.completeness_status,
        "comparability_status": group.comparability_status,
        "finding_status": group.finding_status,
        "expected": group.expected_value,
        "actual": group.actual_value,
        "delta": group.delta,
        "confidence": group.confidence,
        "review_priority": group.review_priority,
        "group_key": group.group_key,
        "needs_reverification": bool(group.needs_reverification),
        "basis_changed_at": group.basis_changed_at,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
        "process_status": group.process.status,
        "inspector_status": inspector_status,
        "decisions": [{"id": row.id, "decision": row.decision, "reason_code": row.reason_code,
                       "comment": row.comment, "user_id": row.user_id, "created_at": row.created_at} for row in decisions],
    }
    if include_fragments:
        fragments = (
            db.query(EvidenceFragment)
            .filter(EvidenceFragment.evidence_group_id == int(group.id))
            .order_by(EvidenceFragment.role.asc(), EvidenceFragment.id.asc())
            .all()
        )
        payload["fragments"] = [evidence_fragment_to_dict(fragment) for fragment in fragments]
    payload["protocol_status"] = evidence_group_to_submission_check(payload)["protocol_status"]
    return payload


def evidence_fragment_to_dict(fragment: EvidenceFragment) -> dict[str, Any]:
    doc = fragment.document_version
    source_metadata = fragment.source_fragment.metadata_json if fragment.source_fragment and isinstance(fragment.source_fragment.metadata_json, dict) else {}
    dataset_stage = getattr(doc, "dataset_stage", None) if doc else None
    if dataset_stage == "RD_ID_MIXED" and fragment.stage in {"project", "working", "as_built"}:
        dataset_stage = dataset_stage_for_internal(fragment.stage)
    return {
        "id": int(fragment.id),
        "role": fragment.role,
        "file": doc.filename if doc else None,
        "file_id": fragment.dataset_file_id or (getattr(doc, "dataset_file_id", None) if doc else None),
        "file_sha256": fragment.file_sha256,
        "stage": fragment.stage,
        "dataset_stage": dataset_stage or dataset_stage_for_internal(fragment.stage),
        "dataset_split": getattr(doc, "dataset_split", None) if doc else source_metadata.get("split"),
        "dataset_section": getattr(doc, "dataset_section", None) if doc else source_metadata.get("section"),
        "discipline": fragment.discipline,
        "document_code": fragment.document_code,
        "revision": fragment.revision,
        "approval_status": fragment.approval_status,
        "page": fragment.page,
        "bbox": fragment.bbox,
        "bbox_normalized": fragment.bbox,
        "coordinate_space": "UNROTATED_PDF" if fragment.extractor == "annotation_context" else "SOURCE_PAGE",
        "bbox_pdf": fragment.bbox_pdf,
        "page_width": fragment.page_width,
        "page_height": fragment.page_height,
        "polygon": fragment.polygon,
        "extracted_value": fragment.extracted_value,
        "context": fragment.context,
        "extractor": fragment.extractor,
        "confidence": fragment.confidence,
        "document_version_id": fragment.document_version_id,
        "source_fragment_id": fragment.source_fragment_id,
    }


def protocol_to_dict(protocol: Protocol | None, *, include_payload: bool = True) -> dict[str, Any] | None:
    if protocol is None:
        return None
    result = {
        "id": int(protocol.id),
        "process_id": protocol.process_id,
        "project_id": protocol.project_id,
        "object_id": protocol.object_id,
        "version": protocol.version,
        "matrix_version": protocol.matrix_version,
        "dataset_version": protocol.dataset_version,
        "model_version": protocol.model_version,
        "input_manifest_hash": protocol.input_manifest_hash,
        "status": protocol.status,
        "created_at": protocol.created_at,
        "finalized_at": protocol.finalized_at,
        "finalized_by_user_id": protocol.finalized_by_user_id,
        "unfinalized_at": protocol.unfinalized_at,
        "unfinalized_by_user_id": protocol.unfinalized_by_user_id,
        "unfinalize_reason": protocol.unfinalize_reason,
    }
    if include_payload:
        # Deferred columns must not be touched when the caller opted out via
        # latest_protocol(include_payload=False); accessing them would trigger a
        # lazy per-row SELECT, defeating the point of deferring it.
        result["payload"] = protocol.payload_json or {}
    return result


def protocol_pdf_bytes(protocol: Protocol) -> bytes:
    return render_protocol_pdf(protocol)


def document_completeness(docs: Iterable[DocumentVersion]) -> dict[str, str]:
    stages = {_doc_stage(doc) for doc in docs}
    return {
        uploaded: uploaded if stage in stages else missing
        for stage, (uploaded, missing) in STAGE_TO_COMPLETENESS.items()
    }


def upload_scenario(completeness: dict[str, str]) -> str:
    has_pd = completeness.get("PD_UPLOADED") == "PD_UPLOADED"
    has_rd = completeness.get("RD_UPLOADED") == "RD_UPLOADED"
    has_id = completeness.get("ID_UPLOADED") == "ID_UPLOADED"
    count = sum([has_pd, has_rd, has_id])
    if count == 3:
        return "FULL"
    if has_pd and has_rd:
        return "PD_RD_ONLY"
    if has_pd and has_id:
        return "PD_ID_ONLY"
    if has_rd and has_id:
        return "RD_ID_ONLY"
    if count == 1:
        return "SINGLE_ONLY"
    return "PARTIALLY_LOADED"


def input_manifest_hash(docs: Iterable[DocumentVersion]) -> str:
    rows = [
        {
            "id": int(doc.id),
            "file_id": doc.dataset_file_id,
            "stage": _doc_stage(doc),
            "dataset_stage": getattr(doc, "dataset_stage", None),
            "code": doc.document_code,
            "revision": doc.revision,
            "approval_status": doc.approval_status,
            "hash": doc.file_hash or doc.content_hash,
        }
        for doc in docs
    ]
    rows.sort(key=lambda item: (str(item["stage"]), str(item["code"]), str(item["revision"]), str(item["id"])))
    return _hash_json(rows)


def current_applicable_documents(docs: list[DocumentVersion]) -> dict[str, DocumentVersion]:
    out: dict[str, DocumentVersion] = {}
    for stage in DOC_STAGES:
        stage_docs = [doc for doc in docs if _doc_stage(doc) == stage and _is_approved(doc)]
        if not stage_docs:
            continue
        stage_docs.sort(key=_doc_revision_key)
        out[stage] = stage_docs[-1]
    return out


def add_audit(
    db: Session,
    *,
    action: str,
    user_id: int | None,
    process: InspectionProcess | None = None,
    object_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    row = AuditLog(
        user_id=user_id,
        action=action,
        object_id=object_id or (process.object_id if process else None),
        project_id=int(process.project_id) if process else None,
        process_id=str(process.id) if process else None,
        details=details or {},
        ip_address=get_client_ip(),
    )
    db.add(row)
    db.flush()
    return row


def _stage_value_fingerprint(value: StageValue, role: str) -> dict[str, Any]:
    return {
        "document_version_id": int(value.document.id),
        "source_fragment_id": int(value.source_fragment.id) if value.source_fragment and value.source_fragment.id is not None else None,
        "page": value.page,
        "extracted_value": value.value,
        "role": role,
        "file_sha256": value.document.file_hash or value.document.content_hash,
        "revision": value.document.revision,
    }


def _create_evidence_group_for_param(
    db: Session,
    process: InspectionProcess,
    param: Param,
    entity: CanonicalEntity | None,
    docs: list[DocumentVersion],
    current_docs: dict[str, DocumentVersion],
    *,
    user_id: int | None = None,
) -> tuple[str, EvidenceGroup | None]:
    """Returns (group_key, group). `group` is None when the evidence basis is
    unchanged since the last run (nothing was touched); the key is still
    returned so the caller can mark it as accounted for this run."""
    needed_stages = _param_needed_stages(param)
    missing_stages = [stage for stage in needed_stages if not any(_doc_stage(doc) == stage for doc in docs)]
    unclear_stages = [
        stage
        for stage in needed_stages
        if any(_doc_stage(doc) == stage for doc in docs) and stage not in current_docs
    ]

    values = {stage: _extract_stage_value(db, param, entity, current_docs.get(stage)) for stage in needed_stages if current_docs.get(stage)}
    expected = _expected_value(values)
    actual = values.get("as_built") if "as_built" in needed_stages else None
    context_values = [value for stage, value in values.items() if value and stage not in {expected.stage if expected else "", actual.stage if actual else ""}]

    status = STATUS_MISSING_EVIDENCE
    comparability = "NOT_COMPARABLE"
    delta: dict[str, Any] | None = None
    confidence = None

    if missing_stages:
        status = STATUS_MISSING_EVIDENCE
        delta = {"reason": "missing_stage", "stages": [STAGE_SHORT.get(stage, stage) for stage in missing_stages]}
    elif unclear_stages:
        status = STATUS_CLARIFICATION_REQUIRED
        delta = {"reason": "no_approved_current_revision", "stages": [STAGE_SHORT.get(stage, stage) for stage in unclear_stages]}
    elif not expected or ("as_built" in needed_stages and not actual):
        status = STATUS_MISSING_EVIDENCE
        delta = {"reason": "value_not_extracted"}
    elif not _values_comparable(param, expected, actual):
        status = STATUS_NOT_COMPARABLE
        delta = {"expected": expected.normalized_value if expected else None, "actual": actual.normalized_value if actual else None}
    else:
        comparability = "COMPARABLE"
        delta = _delta(param, expected, actual)
        status = STATUS_NEGATIVE_VERIFIED if delta.get("equal") else STATUS_CANDIDATE
        confidence = min(float(expected.confidence or 0), float(actual.confidence or 0)) if actual else float(expected.confidence or 0)

    write_expected = bool(expected)
    write_actual = bool(actual and (not expected or actual.source_fragment != expected.source_fragment))
    fragment_plan: list[tuple[StageValue, str]] = []
    if write_expected:
        fragment_plan.append((expected, "expected"))
    if write_actual:
        fragment_plan.append((actual, "actual"))
    for value in context_values:
        fragment_plan.append((value, "context"))
    fragment_specs = [_stage_value_fingerprint(value, role) for value, role in fragment_plan]

    group_key = f"entity:{entity.id}" if entity else "entity:none"
    group, should_write_fragments = upsert_evidence_group(
        db, process, param, group_key,
        fields={
            "canonical_entity_id": entity.id if entity else None,
            "matrix_version": str(process.matrix_version or MATRIX_VERSION_DEMO),
            "model_version": str(process.model_version or MODEL_VERSION_RULES),
            "dataset_version": str(process.dataset_version or DATASET_VERSION_LOCAL),
            "comparison_scenario": str(process.upload_scenario or "SINGLE_ONLY"),
            "completeness_status": "COMPLETE" if not missing_stages else "PARTIALLY_LOADED",
            "comparability_status": comparability,
            "finding_status": status,
            "expected_value": expected.value if expected else None,
            "actual_value": actual.value if actual else None,
            "delta": delta,
            "review_priority": _priority(param.review_priority),
            "confidence": confidence,
        },
        fragment_specs=fragment_specs,
        user_id=user_id,
    )
    if group is not None and should_write_fragments:
        for value, role in fragment_plan:
            _add_fragment(db, group, value, role)
        db.flush()
    return group_key, group


def _create_not_applicable_group(db: Session, process: InspectionProcess, param: Param, *, user_id: int | None = None) -> tuple[str, EvidenceGroup | None]:
    group_key = "not_applicable"
    group, _should_write_fragments = upsert_evidence_group(
        db, process, param, group_key,
        fields={
            "canonical_entity_id": None,
            "matrix_version": str(process.matrix_version or MATRIX_VERSION_DEMO),
            "model_version": str(process.model_version or MODEL_VERSION_RULES),
            "dataset_version": str(process.dataset_version or DATASET_VERSION_LOCAL),
            "comparison_scenario": str(process.upload_scenario or "SINGLE_ONLY"),
            "completeness_status": "COMPLETE",
            "comparability_status": "NOT_APPLICABLE",
            "finding_status": STATUS_NOT_APPLICABLE,
            "delta": {"reason": "no_matching_entity_type"},
            "review_priority": _priority(param.review_priority),
        },
        fragment_specs=[],
        user_id=user_id,
    )
    return group_key, group


def _extract_stage_value(
    db: Session,
    param: Param,
    entity: CanonicalEntity | None,
    document: DocumentVersion | None,
) -> StageValue | None:
    if not document:
        return None
    attr_name = _trigger_value(param.trigger_logic, "attribute")
    if entity is not None and attr_name:
        row = (
            db.query(AttributeObservation, EntityObservation, SourceFragment)
            .join(EntityObservation, AttributeObservation.entity_observation_id == EntityObservation.id)
            .outerjoin(SourceFragment, AttributeObservation.source_fragment_id == SourceFragment.id)
            .filter(
                EntityObservation.canonical_entity_id == entity.id,
                EntityObservation.document_version_id == int(document.id),
                AttributeObservation.attribute_name == attr_name,
            )
            .order_by(AttributeObservation.confidence.desc().nullslast(), AttributeObservation.id.desc())
            .first()
        )
        if row:
            attr, _obs, fragment = row
            raw = attr.raw_value or attr.normalized_value or attr.normalized_numeric
            normalized = attr.normalized_value or (str(attr.normalized_numeric) if attr.normalized_numeric is not None else str(raw or ""))
            return StageValue(
                stage=_doc_stage(document),
                document=document,
                value=_with_unit(raw, attr.unit),
                normalized_value=str(normalized or ""),
                confidence=float(attr.confidence or 0.8),
                extractor=str(attr.extractor or "attribute_observation"),
                source_fragment=fragment,
                page=int(fragment.page) if fragment and fragment.page is not None else None,
                bbox=_normalize_bbox(fragment.bbox, fragment.metadata_json) if fragment else None,
                context=fragment.text if fragment else None,
            )

    fragments = (
        db.query(SourceFragment)
        .filter(SourceFragment.document_version_id == int(document.id))
        .order_by(SourceFragment.confidence.desc().nullslast(), SourceFragment.id.asc())
        .all()
    )
    results: list[ExtractionResult] = []
    for extractor in default_extractors():
        results.extend(extractor.extract(param=param, document=document, fragments=fragments))
    if not results:
        return None
    best = sorted(results, key=lambda item: float(item.confidence or 0), reverse=True)[0]
    fragment = db.get(SourceFragment, int(best.source_fragment_id)) if best.source_fragment_id else None
    return StageValue(
        stage=_doc_stage(document),
        document=document,
        value=best.value,
        normalized_value=best.normalized_value,
        confidence=float(best.confidence or 0.7),
        extractor=best.extractor,
        source_fragment=fragment,
        page=best.page,
        bbox=best.bbox,
        context=best.context,
    )


def _add_fragment(db: Session, group: EvidenceGroup, value: StageValue, role: str) -> EvidenceFragment:
    metadata = value.source_fragment.metadata_json if value.source_fragment and isinstance(value.source_fragment.metadata_json, dict) else {}
    fragment = EvidenceFragment(
        evidence_group_id=int(group.id),
        document_version_id=int(value.document.id),
        source_fragment_id=int(value.source_fragment.id) if value.source_fragment and value.source_fragment.id is not None else None,
        file_sha256=value.document.file_hash or value.document.content_hash,
        dataset_file_id=getattr(value.document, "dataset_file_id", None),
        stage=_doc_stage(value.document),
        discipline=value.document.discipline,
        document_code=value.document.document_code,
        revision=value.document.revision,
        approval_status=value.document.approval_status,
        page=value.page,
        bbox=value.bbox,
        bbox_pdf=getattr(value.source_fragment, "bbox_pdf", None) if value.source_fragment else metadata.get("bbox_pdf"),
        page_width=getattr(value.source_fragment, "page_width", None) if value.source_fragment else metadata.get("page_width"),
        page_height=getattr(value.source_fragment, "page_height", None) if value.source_fragment else metadata.get("page_height"),
        polygon=_bbox_to_polygon(value.bbox),
        extracted_value=value.value,
        role=role,
        context=value.context,
        extractor=value.extractor,
        confidence=value.confidence,
    )
    db.add(fragment)
    db.flush()
    return fragment


def pending_candidate_count(db: Session, process: InspectionProcess) -> int:
    return int(
        db.query(func.count(EvidenceGroup.id))
        .filter(EvidenceGroup.process_id == process.id, EvidenceGroup.finding_status == STATUS_CANDIDATE)
        .scalar()
        or 0
    )


def _update_process_after_decision(db: Session, process: InspectionProcess, *, user_id: int | None = None) -> None:
    pending_candidates = pending_candidate_count(db, process)
    next_status = PROCESS_COMPLETED if pending_candidates == 0 else PROCESS_VERIFYING
    _set_process_status(db, process, next_status, user_id=user_id, details={"pending_candidates": pending_candidates})


def _upsert_gold_draft_item(db: Session, group: EvidenceGroup, decision: EvidenceDecision) -> GoldDraftItem:
    existing = (
        db.query(GoldDraftItem)
        .filter(GoldDraftItem.evidence_group_id == int(group.id))
        .order_by(GoldDraftItem.id.desc())
        .first()
    )
    payload = _json_safe(evidence_group_to_dict(db, group, include_fragments=True))
    if existing:
        existing.decision_id = int(decision.id)
        existing.label = str(group.finding_status)
        existing.payload_json = payload
        db.add(existing)
        db.flush()
        return existing
    row = GoldDraftItem(
        evidence_group_id=int(group.id),
        decision_id=int(decision.id),
        project_id=int(group.project_id),
        organization_id=int(group.organization_id),
        object_id=group.object_id,
        label=str(group.finding_status),
        payload_json=payload,
    )
    db.add(row)
    db.flush()
    return row


def _record_rejection_log(db: Session, process: InspectionProcess, group: EvidenceGroup, decision: EvidenceDecision) -> RejectionLog:
    """ТЗ-10 `Rejection_Log`: one row per inspector rejection (decision ==
    "Reject", finding_status -> NEGATIVE_VERIFIED), for Module 9.4's
    retraining pipeline. `ai_verdict`/`suggested_fix` are left NULL -- Module
    9.4's automated AI-verdict reasoning is not implemented in this codebase
    (see the `RejectionLog` docstring); this only records what an inspector
    actually did, honestly, not a verdict nobody computed."""
    row = RejectionLog(
        evidence_group_id=int(group.id),
        evidence_decision_id=int(decision.id),
        process_id=str(process.id),
        project_id=int(process.project_id),
        organization_id=int(process.organization_id),
        rejection_reason=str(decision.comment or decision.reason_code or "").strip() or (decision.reason_code or "OTHER"),
        retraining_status="PENDING",
    )
    db.add(row)
    db.flush()
    return row


def _delete_existing_gold_fixture_groups(db: Session, process: InspectionProcess, target_codes: set[str]) -> None:
    rows = db.query(EvidenceGroup).filter(EvidenceGroup.process_id == process.id).all()
    for group in rows:
        delta = group.delta if isinstance(group.delta, dict) else {}
        if delta.get("source") != "gold_fixture":
            continue
        code = str(delta.get("parameter_code") or "")
        if target_codes and code not in target_codes:
            continue
        db.delete(group)
    db.flush()


def _create_gold_fixture_groups(
    db: Session,
    process: InspectionProcess,
    params_by_code: dict[str, Param],
    *,
    target_codes: set[str],
) -> list[EvidenceGroup]:
    fixtures = _gold_fixtures_for_process(db, process)
    created: list[EvidenceGroup] = []
    for fixture in fixtures:
        parameter_code = str(fixture.parameter_code or "")
        if target_codes and parameter_code not in target_codes:
            continue
        if str(fixture.matrix_scope or "").upper() == "MATRIX" and parameter_code not in params_by_code:
            continue
        payload = fixture.payload_json if isinstance(fixture.payload_json, dict) else {}
        entity = _ensure_gold_fixture_entity(db, process, fixture)
        param = params_by_code.get(parameter_code)
        finding_status = _fixture_finding_status(fixture)
        expected_value = payload.get("pd_value") or payload.get("rd_value")
        actual_value = payload.get("rd_value") or payload.get("id_value")
        group = EvidenceGroup(
            process_id=str(process.id),
            project_id=int(process.project_id),
            organization_id=int(process.organization_id),
            object_id=process.object_id,
            canonical_entity_id=entity.id,
            param_id=int(param.id) if param else None,
            matrix_version=str(process.matrix_version or MATRIX_VERSION_OFFICIAL),
            model_version=str(process.model_version or MODEL_VERSION_RULES),
            dataset_version=str(process.dataset_version or DATASET_VERSION_OFFICIAL),
            comparison_scenario=str(process.upload_scenario or "SINGLE_ONLY"),
            completeness_status=str(payload.get("document_status") or "COMPLETE"),
            comparability_status="COMPARABLE" if finding_status in {STATUS_CANDIDATE, STATUS_NEGATIVE_VERIFIED, STATUS_SUSPICION} else "NOT_COMPARABLE",
            finding_status=finding_status,
            expected_value=str(expected_value) if expected_value is not None else None,
            actual_value=str(actual_value) if actual_value is not None else None,
            delta={
                "source": "gold_fixture",
                "source_dataset": fixture.source_dataset,
                "check_id": fixture.check_id,
                "finding_group_id": fixture.finding_group_id,
                "parameter_code": parameter_code,
                "matrix_scope": fixture.matrix_scope,
                "location_type": fixture.location_type,
                "location": fixture.location,
                "violation_label": fixture.violation_label,
                "protocol_status": fixture.protocol_status,
                "comparison_result": payload.get("comparison_result"),
                "review_note": payload.get("review_note"),
                "equal": str(fixture.violation_label or "").upper() == "NO_VIOLATION",
            },
            review_priority=_priority(param.review_priority if param else _priority_from_protocol_status(fixture.protocol_status)),
            confidence=0.95 if str(fixture.gold_status or "").upper() == "FINAL_GOLD_EXISTENCE" else 0.75,
        )
        db.add(group)
        db.flush()
        _add_fixture_fragments(db, group, fixture)
        created.append(group)
    db.flush()
    return created


def _gold_fixtures_for_process(db: Session, process: InspectionProcess) -> list[GoldCheckFixture]:
    rows = (
        db.query(GoldCheckFixture)
        .filter(
            GoldCheckFixture.project_id == int(process.project_id),
            GoldCheckFixture.organization_id == int(process.organization_id),
            GoldCheckFixture.object_id == str(process.object_id or ""),
            GoldCheckFixture.evaluation_allowed == True,  # noqa: E712
        )
        .order_by(GoldCheckFixture.source_dataset.asc(), GoldCheckFixture.check_id.asc())
        .all()
    )
    preferred: dict[str, GoldCheckFixture] = {}
    for row in rows:
        existing = preferred.get(str(row.check_id))
        if existing is None or existing.source_dataset != "public_gold":
            preferred[str(row.check_id)] = row
    return [preferred[key] for key in sorted(preferred)]


def _ensure_gold_fixture_entity(db: Session, process: InspectionProcess, fixture: GoldCheckFixture) -> CanonicalEntity:
    entity_id = _fixture_entity_id(fixture)
    existing = db.get(CanonicalEntity, entity_id)
    if existing:
        return existing
    location_type = str(fixture.location_type or "location").lower()
    location = str(fixture.location or fixture.check_id)
    entity = CanonicalEntity(
        id=entity_id,
        project_id=int(process.project_id),
        organization_id=int(process.organization_id),
        entity_type=location_type,
        canonical_name=f"{fixture.object_id} {fixture.parameter_code} {location_type} {location}",
        status="active",
    )
    db.add(entity)
    db.flush()
    alias = EntityAlias(
        canonical_entity_id=entity.id,
        alias=location,
        normalized_alias=_normalize_compare_text(location),
        alias_type=location_type,
        confidence=1.0,
        source="gold_fixture",
    )
    db.add(alias)
    db.flush()
    return entity


def _fixture_entity_id(fixture: GoldCheckFixture) -> str:
    base = f"{fixture.object_id}-{fixture.check_id}"
    if len(base) <= 64:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return f"{str(fixture.object_id)[:45]}-{digest}"


def _fixture_finding_status(fixture: GoldCheckFixture) -> str:
    scope = str(fixture.matrix_scope or "").upper()
    label = str(fixture.violation_label or "").upper()
    if scope != "MATRIX":
        return STATUS_SUSPICION
    if label == "VIOLATION_PRESENT":
        return STATUS_CANDIDATE
    if label == "NO_VIOLATION":
        return STATUS_NEGATIVE_VERIFIED
    if label == "MISSING_DOCUMENT":
        return STATUS_MISSING_EVIDENCE
    return STATUS_NOT_COMPARABLE


def _priority_from_protocol_status(protocol_status: object) -> str:
    status = str(protocol_status or "").upper()
    if status == "CRITICAL":
        return "HIGH"
    if status in {"WARNING", "COMPARISON_IMPOSSIBLE"}:
        return "MEDIUM"
    return "LOW"


def _add_fixture_fragments(db: Session, group: EvidenceGroup, fixture: GoldCheckFixture) -> None:
    payload = fixture.payload_json if isinstance(fixture.payload_json, dict) else {}
    evidence_rows = fixture.evidence_json if isinstance(fixture.evidence_json, list) else []
    for index, evidence in enumerate(evidence_rows):
        if not isinstance(evidence, dict):
            continue
        doc = _document_for_fixture_evidence(db, group, evidence)
        if not doc:
            continue
        source_fragment = _best_source_fragment_for_fixture(db, doc, fixture, evidence)
        stage = _stage_from_fixture_evidence(evidence, doc)
        value = _value_for_fixture_stage(payload, evidence.get("stage"))
        bbox = source_fragment.bbox if source_fragment and source_fragment.bbox else [0.0, 0.0, 1.0, 1.0]
        fragment = EvidenceFragment(
            evidence_group_id=int(group.id),
            document_version_id=int(doc.id),
            source_fragment_id=int(source_fragment.id) if source_fragment else None,
            file_sha256=doc.file_hash or doc.content_hash,
            dataset_file_id=getattr(doc, "dataset_file_id", None),
            stage=stage,
            discipline=doc.discipline,
            document_code=doc.document_code,
            revision=doc.revision,
            approval_status=doc.approval_status,
            page=_optional_int(evidence.get("pdf_page_number")) or (source_fragment.page if source_fragment else None),
            bbox=_normalize_bbox(bbox),
            bbox_pdf=getattr(source_fragment, "bbox_pdf", None) if source_fragment else None,
            page_width=getattr(source_fragment, "page_width", None) if source_fragment else None,
            page_height=getattr(source_fragment, "page_height", None) if source_fragment else None,
            polygon=_bbox_to_polygon(_normalize_bbox(bbox)),
            extracted_value=str(value) if value is not None else None,
            role=_role_for_stage(evidence.get("stage"), index),
            context=(source_fragment.text if source_fragment else None) or str(evidence.get("localization") or ""),
            extractor="gold_fixture",
            confidence=0.95,
        )
        db.add(fragment)
    db.flush()


def _document_for_fixture_evidence(db: Session, group: EvidenceGroup, evidence: dict[str, Any]) -> DocumentVersion | None:
    file_id = str(evidence.get("file_id") or "")
    query = db.query(DocumentVersion).filter(
        DocumentVersion.project_id == int(group.project_id),
        DocumentVersion.organization_id == int(group.organization_id),
        DocumentVersion.object_id == str(group.object_id or ""),
        DocumentVersion.dataset_file_id == file_id,
    )
    return query.first()


def _best_source_fragment_for_fixture(
    db: Session,
    doc: DocumentVersion,
    fixture: GoldCheckFixture,
    evidence: dict[str, Any],
) -> SourceFragment | None:
    page = _optional_int(evidence.get("pdf_page_number"))
    rows = (
        db.query(SourceFragment)
        .filter(SourceFragment.document_version_id == int(doc.id), SourceFragment.page == page)
        .all()
    )
    if not rows:
        return None

    def score(fragment: SourceFragment) -> tuple[int, float]:
        metadata = fragment.metadata_json if isinstance(fragment.metadata_json, dict) else {}
        if fragment.source_system == "learning_annotation" and metadata.get("check_id") == fixture.check_id:
            return (0, -float(fragment.confidence or 0))
        if fragment.source_system == "learning_annotation" and metadata.get("code") == fixture.parameter_code:
            return (1, -float(fragment.confidence or 0))
        if fragment.source_system == "learning_page_index":
            return (2, 0)
        return (3, -float(fragment.confidence or 0))

    return sorted(rows, key=score)[0]


def _stage_from_fixture_evidence(evidence: dict[str, Any], doc: DocumentVersion) -> str:
    stage = str(evidence.get("stage") or "").upper()
    if stage == "PD":
        return "project"
    if stage == "RD":
        return "working"
    if stage == "ID":
        return "as_built"
    return _doc_stage(doc)


def _role_for_stage(stage: object, index: int) -> str:
    value = str(stage or "").upper()
    if value == "PD" or index == 0:
        return "expected"
    if value in {"RD", "ID"}:
        return "actual"
    return "context"


def _value_for_fixture_stage(payload: dict[str, Any], stage: object) -> Any:
    value = str(stage or "").upper()
    if value == "PD":
        return payload.get("pd_value")
    if value == "RD":
        return payload.get("rd_value")
    if value == "ID":
        return payload.get("id_value")
    return payload.get("pd_value") or payload.get("rd_value") or payload.get("id_value")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _set_process_status(
    db: Session,
    process: InspectionProcess,
    status: str,
    *,
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    previous_status = process.status
    process.status = status
    process.updated_at = datetime.utcnow()
    db.add(process)
    db.flush()
    add_audit(db, action="PROCESS_STATUS", user_id=user_id, process=process, details={"status": status, **(details or {})})
    logger.info(
        json.dumps(
            {
                "event": "case10_process_status_transition",
                "process_id": str(process.id),
                "job_id": (details or {}).get("job_id"),
                "attempt": (details or {}).get("attempt"),
                "previous_status": previous_status,
                "status": status,
                "timestamp": process.updated_at.isoformat() if process.updated_at else None,
                "details": details or {},
            },
            ensure_ascii=False,
            default=str,
        )
    )
    publish_process_event({"process_id": process.id, "status": status, **(details or {})})


def _project_documents(db: Session, process: InspectionProcess) -> list[DocumentVersion]:
    query = (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.project_id == int(process.project_id),
            DocumentVersion.organization_id == int(process.organization_id),
        )
    )
    if process.object_id:
        query = query.filter(DocumentVersion.object_id == process.object_id)
    return query.order_by(DocumentVersion.uploaded_at.asc(), DocumentVersion.id.asc()).all()


def _entities_for_process(db: Session, process: InspectionProcess) -> list[CanonicalEntity | None]:
    rows = (
        db.query(CanonicalEntity)
        .filter(
            CanonicalEntity.project_id == int(process.project_id),
            CanonicalEntity.organization_id == int(process.organization_id),
        )
        .order_by(CanonicalEntity.id.asc())
        .all()
    )
    return rows or [None]


def _target_entities_for_param(param: Param, entities: list[CanonicalEntity | None]) -> list[CanonicalEntity | None]:
    entity_type = _trigger_value(param.trigger_logic, "entity_type")
    if not entity_type:
        return entities
    return [entity for entity in entities if entity is not None and str(entity.entity_type or "").lower() == entity_type.lower()]


def _param_needed_stages(param: Param) -> list[str]:
    stages: list[str] = []
    for stage, field in STAGE_TO_SOURCE_FIELD.items():
        if bool(getattr(param, field, False)):
            stages.append(stage)
    return stages or list(DOC_STAGES)


def _expected_value(values: dict[str, StageValue | None]) -> StageValue | None:
    return values.get("working") or values.get("project")


def _values_comparable(param: Param, expected: StageValue | None, actual: StageValue | None) -> bool:
    if expected is None:
        return False
    if actual is None:
        return "as_built" not in _param_needed_stages(param)
    if not expected.normalized_value or not actual.normalized_value:
        return False
    if str(param.data_type or "").lower() == "number":
        return _to_float(expected.normalized_value) is not None and _to_float(actual.normalized_value) is not None
    return True


def _delta(param: Param, expected: StageValue, actual: StageValue | None) -> dict[str, Any]:
    if actual is None:
        return {"expected": expected.normalized_value, "actual": None, "equal": True}
    if str(param.data_type or "").lower() == "number":
        exp = _to_float(expected.normalized_value)
        act = _to_float(actual.normalized_value)
        if exp is None or act is None:
            return {"expected": expected.normalized_value, "actual": actual.normalized_value, "equal": False}
        return {
            "expected": exp,
            "actual": act,
            "delta": act - exp,
            "equal": abs(act - exp) < 1e-9,
        }
    exp_text = _normalize_compare_text(expected.normalized_value)
    act_text = _normalize_compare_text(actual.normalized_value)
    return {
        "expected": expected.normalized_value,
        "actual": actual.normalized_value,
        "equal": exp_text == act_text,
    }


def _status_counts(db: Session, process: InspectionProcess) -> dict[str, int]:
    rows = (
        db.query(EvidenceGroup.finding_status, func.count(EvidenceGroup.id))
        .filter(EvidenceGroup.process_id == process.id)
        .group_by(EvidenceGroup.finding_status)
        .all()
    )
    counts = {status: 0 for status in sorted(FINDING_STATUSES)}
    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


def _count_by(groups: list[EvidenceGroup], attr: str) -> dict[str, int]:
    counts = Counter(str(getattr(group, attr) or "") for group in groups)
    return dict(sorted(counts.items()))


def _require_process(db: Session, process_id: str) -> InspectionProcess:
    process = db.get(InspectionProcess, str(process_id))
    if not process:
        raise HTTPException(status_code=404, detail="Inspection process not found")
    return process


def _db_cache_get(db: Session, cache_key: str) -> dict[str, Any] | None:
    row = db.query(ProcessingCache).filter(ProcessingCache.cache_key == cache_key).first()
    return row.result_json if row and isinstance(row.result_json, dict) else None


def _db_cache_set(db: Session, cache_key: str, result: dict[str, Any]) -> None:
    input_hash = cache_key.split(":")[2] if ":" in cache_key else cache_key
    row = db.query(ProcessingCache).filter(ProcessingCache.cache_key == cache_key).first()
    if row:
        row.input_hash = input_hash
        row.result_json = result
        db.add(row)
        db.flush()
        return
    db.add(ProcessingCache(cache_key=cache_key, input_hash=input_hash, result_json=result))
    db.flush()


def _doc_stage(doc: DocumentVersion) -> str:
    return str(doc.doc_stage or doc.document_stage or "unknown")


def _is_approved(doc: DocumentVersion) -> bool:
    return _approval_status(doc.approval_status) in APPROVED_STATUSES


def _approval_status(value: object) -> str:
    text = str(value or "UNKNOWN").strip().upper().replace("Ё", "Е")
    if text in {"APPROVED", "SIGNED", "ISSUED_FOR_CONSTRUCTION", "УТВЕРЖДЕН", "УТВЕРЖДЕНО", "СОГЛАСОВАНО"}:
        return text
    if text in {"UNKNOWN", "", "NONE", "NULL"}:
        return "UNKNOWN"
    return text


def _doc_revision_key(doc: DocumentVersion) -> tuple[int, datetime, int]:
    return (_revision_number(doc.revision), doc.approval_date or doc.uploaded_at or doc.created_at or datetime.min, int(doc.id or 0))


def _revision_number(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return -1
    match = re.search(r"\d+", text)
    if match:
        return int(match.group(0))
    return sum(ord(ch) for ch in text)


def _trigger_value(trigger_logic: object, key: str) -> str | None:
    text = str(trigger_logic or "")
    match = re.search(rf"(?:^|[;\s]){re.escape(key)}\s*=\s*([^;]+)", text)
    return match.group(1).strip() if match else None


def _priority(value: object) -> str:
    priority = str(value or "MEDIUM").strip().upper()
    return priority if priority in REVIEW_PRIORITIES else "MEDIUM"


def _to_float(value: object) -> float | None:
    text = str(value or "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _with_unit(value: object, unit: object) -> str:
    text = str(value or "").strip()
    unit_text = str(unit or "").strip()
    if not text or not unit_text:
        return text
    return text if text.lower().endswith(unit_text.lower()) else f"{text} {unit_text}"


def _normalize_compare_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _bbox_to_polygon(bbox: list[float] | None) -> list[list[float]] | None:
    if not bbox or len(bbox) < 4:
        return None
    x1, y1, x2, y2 = bbox[:4]
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def _hash_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _ascii_pdf_line(line: str) -> str:
    encoded = str(line or "").encode("latin-1", "replace")
    return encoded.decode("latin-1")
