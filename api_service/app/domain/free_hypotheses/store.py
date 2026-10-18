"""Persistence of free hypotheses: `EvidenceGroup` (finding_status=SUSPICION, matrix_scope=FREE_SEARCH) + the
ТЗ-10 `Suspicions` row, written through the same incremental upsert as every other mechanism.

Rules that keep the inspector's work and the counts honest:
  * a hypothesis hangs on an inactive sentinel parameter (`FREE-SEARCH`), so it is never one of the matrix parameters and
    the matrix size / "checked parameters" statistics do not see it;
  * `Suspicions.inspector_status` (the review of the hypothesis) is never overwritten by a re-run; when the evidence
    behind a reviewed hypothesis changes, the new values are parked under `delta.pending_reverification` and the row is
    flagged, exactly like a decided finding (`evidence_groups.upsert_evidence_group`);
  * a hypothesis the inspector converted to a CANDIDATE is never turned back into a SUSPICION by a re-run;
  * a hypothesis not produced by the current run is removed (nothing decided) or kept and flagged (reviewed / decided).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from ...db.models import EvidenceDecision, EvidenceFragment, EvidenceGroup, InspectionProcess, MatrixVersion, Param, Suspicion
from ..evidence_groups import compute_basis_hash, upsert_evidence_group
from .base import (
    FREE_SEARCH_PARAM_CODE,
    FREE_SEARCH_PARAM_NAME,
    FREE_SEARCH_SECTION,
    GROUP_KEY_PREFIX,
    MODEL_VERSION,
    EvidenceRef,
    HypothesisCandidate,
    document_reference,
    group_key_for,
)

CONVERTED_SCOPE = "OUT_OF_MATRIX"


def ensure_free_search_param(db: Session, process: InspectionProcess, params: Iterable[Param] = ()) -> Param:
    """The inactive sentinel parameter of the process's matrix version (created on first use)."""
    matrix_version_id = next((int(p.matrix_version_id) for p in params if p.matrix_version_id is not None), None)
    if matrix_version_id is None:
        version = (
            db.query(MatrixVersion)
            .filter(MatrixVersion.organization_id == int(process.organization_id), MatrixVersion.version == str(process.matrix_version))
            .order_by(MatrixVersion.id.desc())
            .first()
        )
        if version is None:
            raise LookupError("matrix version of the process not found")
        matrix_version_id = int(version.id)
    param = db.query(Param).filter(Param.matrix_version_id == matrix_version_id, Param.code == FREE_SEARCH_PARAM_CODE).first()
    if param is None:
        param = Param(
            matrix_version_id=matrix_version_id, project_id=int(process.project_id), organization_id=int(process.organization_id),
            code=FREE_SEARCH_PARAM_CODE, section=FREE_SEARCH_SECTION, parameter_name=FREE_SEARCH_PARAM_NAME,
            source_pd=True, source_rd=True, source_id=True, review_priority="MEDIUM", data_type="string",
            trigger_logic="Гипотеза вне Матрицы: параметра и порога нет; основание указано в описании.", is_active=False,
        )
        db.add(param)
        db.flush()
    return param


def _stage_of_document(document: Any) -> str | None:
    return {"PD": "project", "RD": "working", "ID": "as_built"}.get(str(getattr(document, "dataset_stage", None) or "").upper()) or getattr(document, "doc_stage", None)


def fragment_spec(ref: EvidenceRef, *, extractor: str, confidence: float) -> dict[str, Any]:
    doc = ref.document
    width, height = ref.page_width, ref.page_height
    bbox = bbox_pdf = polygon = None
    if ref.bbox_pdf and width and height:
        x0, y0, x1, y1 = (float(v) for v in ref.bbox_pdf)
        bbox_pdf = [round(v, 2) for v in (x0, y0, x1, y1)]
        bbox = [round(min(1.0, max(0.0, v)), 6) for v in (x0 / width, y0 / height, x1 / width, y1 / height)]
        polygon = [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]]
    return {
        "document_version_id": int(doc.id), "source_fragment_id": None,
        "dataset_file_id": getattr(doc, "dataset_file_id", None), "file_sha256": getattr(doc, "file_hash", None) or getattr(doc, "content_hash", None),
        "stage": _stage_of_document(doc) or {"PD": "project", "RD": "working", "ID": "as_built"}.get(ref.stage),
        "discipline": getattr(doc, "discipline", None), "document_code": getattr(doc, "document_code", None),
        "revision": getattr(doc, "revision", None), "approval_status": getattr(doc, "approval_status", None),
        "page": int(ref.page), "bbox": bbox, "bbox_pdf": bbox_pdf, "page_width": width, "page_height": height, "polygon": polygon,
        "extracted_value": ref.value, "role": ref.role, "context": (ref.context or "")[:1000] or None,
        "extractor": extractor, "confidence": ref.confidence if ref.confidence is not None else confidence,
    }


def _reference(candidate: HypothesisCandidate, stage: str) -> str | None:
    refs = [ref for ref in candidate.evidence if ref.stage == stage]
    return document_reference(refs[0]) if refs else None


def candidate_delta(candidate: HypothesisCandidate) -> dict[str, Any]:
    pd_ref = _reference(candidate, "PD")
    rd_ref = _reference(candidate, "RD") or _reference(candidate, "ID")
    stages = sorted({ref.stage for ref in candidate.evidence if ref.role in ("expected", "actual")})
    delta = {
        "source": "free_hypotheses", "matrix_scope": "FREE_SEARCH", "discovery_method": candidate.method,
        "rule_id": candidate.detector, "rule_name": candidate.details.get("rule_name") or candidate.detector,
        "description": candidate.description, "location": candidate.location,
        "pd_reference": pd_ref, "rd_reference": rd_ref, "criticality": candidate.criticality,
        "normative_base": candidate.normative_base or "не определено",
        "inspector_status": "PENDING", "confidence_kind": "ранжирующая оценка, не вероятность нарушения",
        "values": {"PD": candidate.expected, "RD": candidate.actual},
        "evidence_stages": stages, "evidence_required": list(candidate.details.get("evidence_required") or []),
        "editions": list(candidate.editions),
        "details": candidate.details,
    }
    return delta


def _find_group(db: Session, process: InspectionProcess, param: Param, group_key: str) -> EvidenceGroup | None:
    return (db.query(EvidenceGroup)
            .filter(EvidenceGroup.process_id == process.id, EvidenceGroup.param_id == int(param.id), EvidenceGroup.group_key == group_key)
            .first())


def _upsert_suspicion_row(db: Session, process: InspectionProcess, group: EvidenceGroup, candidate: HypothesisCandidate) -> Suspicion:
    row = db.query(Suspicion).filter(Suspicion.evidence_group_id == int(group.id)).first()
    if row is None:
        row = Suspicion(evidence_group_id=int(group.id))
    row.process_id = str(process.id)
    row.project_id = int(process.project_id)
    row.organization_id = int(process.organization_id)
    row.object_id = group.object_id
    row.discovery_method = candidate.method
    row.confidence = candidate.confidence
    row.description = candidate.description
    row.pd_reference = _reference(candidate, "PD")
    row.rd_reference = _reference(candidate, "RD") or _reference(candidate, "ID")
    row.criticality = candidate.criticality
    row.normative_base = candidate.normative_base or "не определено"
    db.add(row)
    db.flush()
    return row


def _is_reviewed(db: Session, group: EvidenceGroup) -> bool:
    row = db.query(Suspicion).filter(Suspicion.evidence_group_id == int(group.id)).first()
    if row is not None and str(row.inspector_status or "PENDING") != "PENDING":
        return True
    return db.query(EvidenceDecision.id).filter(EvidenceDecision.evidence_group_id == int(group.id)).first() is not None


def store_candidates(db: Session, process: InspectionProcess, param: Param, candidates: list[HypothesisCandidate], *,
                     user_id: int | None = None) -> dict[str, Any]:
    """Upsert one group per candidate. Returns {"keys": produced group keys, "created", "unchanged", "flagged"}."""
    keys: set[str] = set()
    stats = {"created": 0, "refreshed": 0, "unchanged": 0, "flagged": 0, "kept_converted": 0}
    for candidate in candidates:
        group_key = group_key_for(candidate)
        keys.add(group_key)
        extractor = f"free_hypotheses.{candidate.detector}"
        specs = [fragment_spec(ref, extractor=extractor, confidence=candidate.confidence) for ref in candidate.evidence]
        delta = candidate_delta(candidate)
        fields = {
            "matrix_version": process.matrix_version, "model_version": MODEL_VERSION, "dataset_version": process.dataset_version,
            "comparison_scenario": process.upload_scenario, "completeness_status": "COMPLETE", "comparability_status": "COMPARABLE",
            "finding_status": "SUSPICION", "expected_value": candidate.expected, "actual_value": candidate.actual,
            "review_priority": candidate.review_priority, "confidence": round(float(candidate.confidence), 4), "delta": delta,
        }
        existing = _find_group(db, process, param, group_key)
        if existing is not None:
            old_delta = existing.delta if isinstance(existing.delta, dict) else {}
            new_hash = compute_basis_hash(expected_value=fields["expected_value"], actual_value=fields["actual_value"], delta=delta, fragment_specs=specs)
            if existing.evidence_basis_hash != new_hash and (old_delta.get("converted_to_candidate") or _is_reviewed(db, existing)):
                # reviewed / converted: never overwritten; the new evidence is parked for the inspector to see
                if old_delta.get("pending_reverification", {}).get("basis_hash") != new_hash:
                    merged = dict(old_delta)
                    merged["pending_reverification"] = {"expected_value": fields["expected_value"], "actual_value": fields["actual_value"],
                                                        "delta": delta, "basis_hash": new_hash, "fragments": specs,
                                                        "computed_at": datetime.utcnow().isoformat()}
                    existing.delta = merged
                    existing.needs_reverification = True
                    existing.basis_changed_at = datetime.utcnow()
                    db.add(existing)
                    db.flush()
                    stats["flagged"] += 1
                else:
                    stats["unchanged"] += 1
                if old_delta.get("converted_to_candidate"):
                    stats["kept_converted"] += 1
                continue
        group, write = upsert_evidence_group(db, process, param, group_key, fields=fields, fragment_specs=specs, user_id=user_id)
        if group is None:
            stats["unchanged"] += 1
            continue
        if write:
            for spec in specs:
                db.add(EvidenceFragment(evidence_group_id=group.id, **spec))
            db.flush()
            stats["created" if existing is None else "refreshed"] += 1
        _upsert_suspicion_row(db, process, group, candidate)
    return {"keys": keys, **stats}


def reconcile_stale(db: Session, process: InspectionProcess, param: Param, produced_keys: set[str], *, ran_methods: Iterable[str]) -> dict[str, int]:
    """Hypotheses of the methods that ran, not produced now: delete when nobody looked at them, keep and flag when a
    review or decision exists. Hypotheses of methods that did not run (disabled / out of scope) are left alone."""
    methods = {f"{GROUP_KEY_PREFIX}{m.lower()}:" for m in ran_methods}
    removed = flagged = 0
    for group in db.query(EvidenceGroup).filter(EvidenceGroup.process_id == process.id, EvidenceGroup.param_id == int(param.id)).all():
        key = str(group.group_key or "")
        if key in produced_keys or not any(key.startswith(prefix) for prefix in methods):
            continue
        delta = group.delta if isinstance(group.delta, dict) else {}
        if delta.get("converted_to_candidate") or _is_reviewed(db, group):
            merged = dict(delta)
            merged["evidence_no_longer_found"] = True
            group.delta = merged
            group.needs_reverification = True
            group.basis_changed_at = datetime.utcnow()
            db.add(group)
            flagged += 1
        else:
            db.delete(group)
            removed += 1
    db.flush()
    return {"removed": removed, "flagged": flagged}


# ------------------------------------------------------------------------------------------------ SUSPICION -> CANDIDATE
class ConversionRefused(ValueError):
    """The hypothesis does not (yet) satisfy the TZ 9.5 conditions for a CANDIDATE; the message names what is missing."""


def conversion_blockers(db: Session, group: EvidenceGroup) -> list[str]:
    """TZ 9.5: "Для перевода в CANDIDATE требуются конкретные источники и координаты доказательств". Empty list = allowed."""
    delta = group.delta if isinstance(group.delta, dict) else {}
    if group.finding_status != "SUSPICION" or delta.get("matrix_scope") != "FREE_SEARCH":
        return ["not_an_open_hypothesis"]
    rows = db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == int(group.id),
                                             EvidenceFragment.role.in_(("expected", "actual"))).all()
    blockers: list[str] = []
    if not rows:
        return ["no_evidence_fragments"]
    for row in rows:
        problems = [name for name, ok in (("file", bool(row.dataset_file_id or row.file_sha256)), ("page", bool(row.page)),
                                          ("bbox", bool(row.bbox or row.bbox_pdf))) if not ok]
        if problems:
            blockers.append(f"fragment {int(row.id)} ({row.role}): missing {', '.join(problems)}")
    required = set(delta.get("evidence_required") or [])
    present = {str(row.stage) for row in rows}
    stage_names = {"PD": "project", "RD": "working", "ID": "as_built"}
    for stage in sorted(required):
        if stage_names.get(stage, stage) not in present:
            blockers.append(f"no evidence from stage {stage}")
    return blockers


def convert_to_candidate(db: Session, group: EvidenceGroup, *, reason: str, user_id: int | None, process: InspectionProcess) -> dict[str, Any]:
    """The inspector's "Преобразовать в кандидата". Refused unless every cited place has file, page and box (and the stages the
    claim needs are cited). The group stays on the sentinel parameter, leaves FREE_SEARCH (so it is not exported as a matrix
    check) and becomes a CANDIDATE; CONFIRMED_VIOLATION remains the inspector's separate decision."""
    blockers = conversion_blockers(db, group)
    if blockers:
        raise ConversionRefused("; ".join(blockers))
    delta = dict(group.delta if isinstance(group.delta, dict) else {})
    delta.update({
        "converted_to_candidate": True, "converted_from": {"finding_status": "SUSPICION", "matrix_scope": delta.get("matrix_scope")},
        "matrix_scope": CONVERTED_SCOPE, "converted_at": datetime.utcnow().isoformat(), "converted_by_user_id": user_id,
        "conversion_reason": reason,
    })
    group.delta = delta
    group.finding_status = "CANDIDATE"
    db.add(group)
    db.flush()
    return {"finding_status": group.finding_status, "matrix_scope": CONVERTED_SCOPE}
