from __future__ import annotations

from collections import Counter
from datetime import datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..db.models import ConstructionObject, DocumentVersion, GoldCheckFixture, MatrixVersion, Param, SourceFragment


MATRIX_VERSION_OFFICIAL = "official-132-v1.1"
DATASET_VERSION_OFFICIAL = "case10-official-public-v1"
PUBLIC_OBJECT_IDS = ("OBJ-TYUMENSKAYA-5-GOLD-SEED", "OBJ-NOVOSLOBODSKAYA")
HIDDEN_OBJECT_IDS = ("OBJ-RECHNIKOV-7-7",)

STAGE_MAP = {
    "PD": "project",
    "RD": "working",
    "ID": "as_built",
    "RD_ID_MIXED": "working",
    "UNKNOWN": "unknown",
}
INTERNAL_TO_DATASET_STAGE = {
    "project": "PD",
    "working": "RD",
    "as_built": "ID",
}


def ensure_official_matrix(
    db: Session,
    *,
    organization_id: int,
    project_id: int | None,
    dataset_root: str | Path | None = None,
) -> MatrixVersion:
    paths = find_dataset_paths(dataset_root)
    catalog_path = paths.get("parameter_catalog")
    if not catalog_path or not catalog_path.exists():
        raise FileNotFoundError("parameter_catalog_132.jsonl not found")

    rows = list(_read_jsonl(catalog_path))
    if len(rows) != 132:
        raise HTTPException(status_code=400, detail=f"Official matrix must contain 132 params, got {len(rows)}")

    matrix_xlsx_path = paths.get("matrix_v11_xlsx")
    matrix_rows = _load_matrix_v11(matrix_xlsx_path) if matrix_xlsx_path and matrix_xlsx_path.exists() else _generated_matrix_v11(rows)
    _validate_matrix_v11(matrix_rows)

    source_hash = _sha256_files([catalog_path, matrix_xlsx_path] if matrix_xlsx_path else [catalog_path])
    matrix = (
        db.query(MatrixVersion)
        .filter(
            MatrixVersion.organization_id == int(organization_id),
            MatrixVersion.project_id == project_id,
            MatrixVersion.version == MATRIX_VERSION_OFFICIAL,
        )
        .first()
    )
    existing_count = 0
    if matrix:
        existing_count = db.query(Param).filter(Param.matrix_version_id == int(matrix.id)).count()
    if matrix and matrix.source_hash == source_hash and existing_count == 132:
        return matrix

    if not matrix:
        matrix = MatrixVersion(
            organization_id=int(organization_id),
            project_id=project_id,
            version=MATRIX_VERSION_OFFICIAL,
            status="ACTIVE",
            source_filename=_display_path(matrix_xlsx_path or catalog_path),
            source_hash=source_hash,
        )
        db.add(matrix)
        db.flush()
    else:
        matrix.status = "ACTIVE"
        matrix.source_filename = _display_path(matrix_xlsx_path or catalog_path)
        matrix.source_hash = source_hash
        db.add(matrix)
        db.flush()
        db.query(Param).filter(Param.matrix_version_id == int(matrix.id)).delete(synchronize_session=False)
        db.flush()

    for row in rows:
        parameter_id = _optional_int(row.get("parameter_id"))
        db.add(_param_from_catalog_row(
            row,
            matrix_row=matrix_rows.get(parameter_id or 0),
            matrix=matrix,
            organization_id=organization_id,
            project_id=project_id,
        ))
    db.flush()
    return matrix


def import_official_dataset(
    db: Session,
    *,
    project_id: int,
    organization_id: int,
    object_ids: Iterable[str] | None = None,
    include_hidden: bool = False,
    include_pages: bool = True,
    include_annotations: bool = True,
    include_gold: bool = True,
    allow_hidden_gold_labels: bool = False,
    dataset_root: str | Path | None = None,
) -> dict[str, Any]:
    paths = find_dataset_paths(dataset_root)
    matrix = ensure_official_matrix(db, organization_id=organization_id, project_id=project_id, dataset_root=dataset_root)
    selected = {str(value).strip() for value in (object_ids or PUBLIC_OBJECT_IDS) if str(value).strip()}
    if include_hidden and not object_ids:
        selected.update(HIDDEN_OBJECT_IDS)
    if not include_hidden:
        selected.difference_update(HIDDEN_OBJECT_IDS)

    summary: dict[str, Any] = {
        "dataset_version": DATASET_VERSION_OFFICIAL,
        "matrix_version": matrix.version,
        "matrix_params": db.query(Param).filter(Param.matrix_version_id == int(matrix.id), Param.is_active == True).count(),  # noqa: E712
        "objects": sorted(selected),
        "document_manifest": {"created": 0, "updated": 0, "skipped": 0},
        "files_index": {"created": 0, "updated": 0, "skipped": 0},
        "page_index": {"created": 0, "updated": 0, "skipped": 0},
        "annotations": {"created": 0, "updated": 0, "skipped": 0},
        "gold_checks": {"created": 0, "updated": 0, "skipped": 0, "training_allowed": 0, "evaluation_allowed": 0},
    }

    manifest_path = paths.get("document_manifest")
    if manifest_path and manifest_path.exists():
        summary["document_manifest"] = _import_document_manifest(
            db,
            manifest_path,
            project_id=project_id,
            organization_id=organization_id,
            object_ids=selected,
        )

    train_dir = paths.get("train_public_data_dir")
    if train_dir and train_dir.exists():
        result = _import_learning_split(
            db,
            train_dir,
            project_id=project_id,
            organization_id=organization_id,
            object_ids=selected,
            include_pages=include_pages,
            include_annotations=include_annotations,
        )
        summary["files_index"] = _merge_counts(summary["files_index"], result["files_index"])
        summary["page_index"] = _merge_counts(summary["page_index"], result["page_index"])
        summary["annotations"] = _merge_counts(summary["annotations"], result["annotations"])

    if include_hidden:
        hidden_dir = paths.get("test_hidden_data_dir")
        if hidden_dir and hidden_dir.exists():
            result = _import_learning_split(
                db,
                hidden_dir,
                project_id=project_id,
                organization_id=organization_id,
                object_ids=selected,
                include_pages=include_pages,
                include_annotations=include_annotations,
            )
            summary["files_index"] = _merge_counts(summary["files_index"], result["files_index"])
            summary["page_index"] = _merge_counts(summary["page_index"], result["page_index"])
            summary["annotations"] = _merge_counts(summary["annotations"], result["annotations"])

    if include_gold:
        public_gold = paths.get("public_gold_checks")
        if public_gold and public_gold.exists():
            summary["gold_checks"] = _merge_counts(
                summary["gold_checks"],
                _import_gold_checks(
                    db,
                    public_gold,
                    source_dataset="public_gold",
                    project_id=project_id,
                    organization_id=organization_id,
                    object_ids=selected,
                    allow_hidden_gold_labels=False,
                ),
            )
        all_gold = paths.get("all_gold_checks")
        if all_gold and all_gold.exists():
            summary["gold_checks"] = _merge_counts(
                summary["gold_checks"],
                _import_gold_checks(
                    db,
                    all_gold,
                    source_dataset="all_gold",
                    project_id=project_id,
                    organization_id=organization_id,
                    object_ids=selected,
                    allow_hidden_gold_labels=allow_hidden_gold_labels,
                ),
            )

    summary["documents_by_object_stage"] = _document_counts(db, project_id=project_id, organization_id=organization_id)
    summary["gold_by_object_code"] = _gold_counts(db, project_id=project_id, organization_id=organization_id)
    return summary


@lru_cache(maxsize=8)
def find_dataset_paths(dataset_root: str | Path | None = None) -> dict[str, Path]:
    roots = _candidate_roots(dataset_root)
    return {
        "parameter_catalog": _find_first(roots, "parameter_catalog_132.jsonl", prefer=("01_participant_package", "02_ФОРМАТ")),
        "document_manifest": _find_first(roots, "document_manifest.jsonl", prefer=("01_participant_package", "02_ФОРМАТ"), avoid=("02_gold_methodology", "ОРГАНИЗАТОР")),
        "submission_schema": _find_first(roots, "submission_schema.json", prefer=("01_participant_package", "02_ФОРМАТ")),
        "split_policy": _find_first(roots, "split_policy.json", prefer=("01_participant_package", "02_ФОРМАТ")),
        "matrix_v11_xlsx": _find_matrix_v11_xlsx(roots),
        "public_train_checks": _find_first(roots, "public_train_checks.jsonl", prefer=("01_participant_package", "02_ФОРМАТ")),
        "public_gold_checks": _find_first(roots, "public_gold_checks.jsonl", prefer=("train_public_203", "data")),
        "all_gold_checks": _find_first(roots, "all_gold_checks.jsonl", prefer=("02_gold_methodology", "ОРГАНИЗАТОР_ЗАКРЫТЫЙ")),
        "train_public_data_dir": _find_data_dir(roots, "train_public_203"),
        "test_hidden_data_dir": _find_data_dir(roots, "test_hidden_213"),
    }


def dataset_stage_for_internal(stage: str | None) -> str | None:
    return INTERNAL_TO_DATASET_STAGE.get(str(stage or ""))


def normalize_dataset_stage(stage: object) -> str:
    raw = str(stage or "").strip().upper()
    return STAGE_MAP.get(raw, "unknown")


def _import_document_manifest(
    db: Session,
    path: Path,
    *,
    project_id: int,
    organization_id: int,
    object_ids: set[str],
) -> dict[str, int]:
    counts = Counter({"created": 0, "updated": 0, "skipped": 0})
    for row in _read_jsonl(path):
        if row.get("object_id") not in object_ids:
            counts["skipped"] += 1
            continue
        _ensure_object(db, row, project_id=project_id, organization_id=organization_id)
        created = _upsert_document_version(db, row, project_id=project_id, organization_id=organization_id, metadata_source="document_manifest")
        counts["created" if created else "updated"] += 1
    db.flush()
    return dict(counts)


def _import_learning_split(
    db: Session,
    data_dir: Path,
    *,
    project_id: int,
    organization_id: int,
    object_ids: set[str],
    include_pages: bool,
    include_annotations: bool,
) -> dict[str, dict[str, int]]:
    files_counts = Counter({"created": 0, "updated": 0, "skipped": 0})
    files_path = data_dir / "files_index.jsonl"
    if files_path.exists():
        for row in _read_jsonl(files_path):
            if row.get("object_id") not in object_ids:
                files_counts["skipped"] += 1
                continue
            _ensure_object(db, row, project_id=project_id, organization_id=organization_id)
            created = _upsert_document_version(db, row, project_id=project_id, organization_id=organization_id, metadata_source="files_index")
            files_counts["created" if created else "updated"] += 1
    db.flush()

    docs = _documents_by_file_id(db, project_id=project_id, organization_id=organization_id)
    existing_fragments = _existing_fragments(db, project_id=project_id, organization_id=organization_id)

    page_counts = Counter({"created": 0, "updated": 0, "skipped": 0})
    if include_pages:
        pages_path = data_dir / "page_index.jsonl"
        if pages_path.exists():
            for row in _read_jsonl(pages_path):
                if row.get("object_id") not in object_ids:
                    page_counts["skipped"] += 1
                    continue
                doc = _doc_for_row(docs, row)
                if not doc:
                    page_counts["skipped"] += 1
                    continue
                external_id = f"PAGE:{row.get('split') or ''}:{row.get('file_id')}:{row.get('source_page_number') or row.get('output_page_number')}"
                created = _upsert_source_fragment(db, existing_fragments, doc, row, source_system="learning_page_index", external_id=external_id)
                page_counts["created" if created else "updated"] += 1

    annotation_counts = Counter({"created": 0, "updated": 0, "skipped": 0})
    if include_annotations:
        annotations_path = data_dir / "annotations.jsonl"
        if annotations_path.exists():
            for row in _read_jsonl(annotations_path):
                if row.get("object_id") not in object_ids:
                    annotation_counts["skipped"] += 1
                    continue
                doc = _doc_for_row(docs, row)
                if not doc:
                    annotation_counts["skipped"] += 1
                    continue
                external_id = str(row.get("annotation_id") or f"ANN:{row.get('split') or ''}:{row.get('file_id')}:{row.get('page_number')}:{row.get('code')}:{annotation_counts.total()}")
                created = _upsert_source_fragment(db, existing_fragments, doc, row, source_system="learning_annotation", external_id=external_id)
                annotation_counts["created" if created else "updated"] += 1

    db.flush()
    return {"files_index": dict(files_counts), "page_index": dict(page_counts), "annotations": dict(annotation_counts)}


def _import_gold_checks(
    db: Session,
    path: Path,
    *,
    source_dataset: str,
    project_id: int,
    organization_id: int,
    object_ids: set[str],
    allow_hidden_gold_labels: bool,
) -> dict[str, int]:
    counts = Counter({"created": 0, "updated": 0, "skipped": 0, "training_allowed": 0, "evaluation_allowed": 0})
    existing = {
        (row.source_dataset, row.check_id): row
        for row in db.query(GoldCheckFixture)
        .filter(
            GoldCheckFixture.project_id == int(project_id),
            GoldCheckFixture.organization_id == int(organization_id),
            GoldCheckFixture.source_dataset == source_dataset,
        )
        .all()
    }
    for payload in _read_jsonl(path):
        object_id = str(payload.get("object_id") or "")
        if object_id not in object_ids:
            counts["skipped"] += 1
            continue
        policy = _gold_policy(payload, source_dataset=source_dataset, allow_hidden_gold_labels=allow_hidden_gold_labels)
        if policy["hidden"] and not policy["evaluation_allowed"]:
            counts["skipped"] += 1
            continue
        if policy["training_allowed"]:
            counts["training_allowed"] += 1
        if policy["evaluation_allowed"]:
            counts["evaluation_allowed"] += 1
        check_id = str(payload.get("check_id") or "")
        if not check_id:
            counts["skipped"] += 1
            continue
        row = existing.get((source_dataset, check_id))
        created = row is None
        if row is None:
            row = GoldCheckFixture(
                project_id=int(project_id),
                organization_id=int(organization_id),
                source_dataset=source_dataset,
                check_id=check_id,
            )
        row.finding_group_id = payload.get("finding_group_id")
        row.object_id = object_id
        row.split = payload.get("split")
        row.visibility = payload.get("visibility")
        row.matrix_scope = payload.get("matrix_scope")
        row.parameter_id = _optional_int(payload.get("parameter_id"))
        row.parameter_code = str(payload.get("parameter_code") or "")
        row.location_type = payload.get("location_type")
        row.location = str(payload.get("location") or "")
        row.violation_label = str(payload.get("violation_label") or "")
        row.protocol_status = payload.get("protocol_status")
        row.criticality = payload.get("criticality")
        row.inspector_status = payload.get("inspector_status")
        row.gold_status = payload.get("gold_status")
        row.score_eligible = bool(payload.get("score_eligible"))
        row.training_allowed = bool(policy["training_allowed"])
        row.evaluation_allowed = bool(policy["evaluation_allowed"])
        row.leakage_guard = policy
        row.evidence_json = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        row.payload_json = payload
        db.add(row)
        counts["created" if created else "updated"] += 1
    db.flush()
    return dict(counts)


def _gold_policy(payload: dict[str, Any], *, source_dataset: str, allow_hidden_gold_labels: bool) -> dict[str, Any]:
    split = str(payload.get("split") or "").upper()
    visibility = str(payload.get("visibility") or "").upper()
    object_id = str(payload.get("object_id") or "")
    hidden = split == "TEST_HIDDEN" or object_id in HIDDEN_OBJECT_IDS
    organizer_only = visibility == "ORGANIZER_ONLY" or source_dataset == "all_gold"
    public_train = split == "TRAIN_PUBLIC"
    training_allowed = public_train and visibility == "PUBLIC_TRAIN_LABEL" and not hidden
    evaluation_allowed = public_train and not hidden
    if hidden:
        evaluation_allowed = bool(allow_hidden_gold_labels)
    blocked_reasons: list[str] = []
    if hidden and not allow_hidden_gold_labels:
        blocked_reasons.append("hidden_labels_blocked")
    if organizer_only:
        blocked_reasons.append("organizer_only_not_training")
    if not public_train:
        blocked_reasons.append("not_public_train")
    return {
        "source_dataset": source_dataset,
        "split": split,
        "visibility": visibility,
        "hidden": hidden,
        "organizer_only": organizer_only,
        "training_allowed": training_allowed,
        "evaluation_allowed": evaluation_allowed,
        "blocked_reasons": blocked_reasons,
    }


def _upsert_document_version(
    db: Session,
    row: dict[str, Any],
    *,
    project_id: int,
    organization_id: int,
    metadata_source: str,
) -> bool:
    file_id = str(row.get("file_id") or "").strip()
    object_id = str(row.get("object_id") or "").strip()
    split = str(row.get("split") or "").strip() or None
    doc = (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.project_id == int(project_id),
            DocumentVersion.organization_id == int(organization_id),
            DocumentVersion.dataset_file_id == file_id,
            DocumentVersion.object_id == object_id,
        )
        .first()
    )
    created = doc is None
    if doc is None:
        doc = DocumentVersion(
            project_id=int(project_id),
            organization_id=int(organization_id),
            source_type="case10_dataset",
            source_document_id=None,
            dataset_file_id=file_id,
            object_id=object_id,
            filename=_filename_from_row(row),
        )
    raw_stage = str(row.get("stage") or "").strip().upper()
    normalized_stage = normalize_dataset_stage(raw_stage)
    sha = row.get("source_sha256") or row.get("sha256")
    section = row.get("section")
    metadata = dict(doc.dataset_metadata or {})
    metadata[metadata_source] = row
    metadata["mixed_stage_requires_review"] = raw_stage == "RD_ID_MIXED"
    doc.filename = _filename_from_row(row)
    doc.doc_stage = normalized_stage
    doc.document_stage = normalized_stage
    doc.dataset_split = split
    doc.dataset_stage = raw_stage or None
    doc.dataset_section = str(section) if section is not None else None
    doc.dataset_metadata = metadata
    doc.discipline = str(section) if section else None
    doc.document_code = file_id or _document_code_from_path(row)
    doc.version = str(row.get("schema_version") or doc.version or "")
    doc.revision = _revision_from_path(row) or doc.revision
    doc.approval_status = _approval_status_for_row(row)
    doc.content_hash = str(sha) if sha else doc.content_hash
    doc.file_hash = str(sha) if sha else doc.file_hash
    doc.file_path = row.get("source_relative_path") or row.get("relative_path") or row.get("output_pdf") or doc.file_path
    db.add(doc)
    db.flush()
    return created


def _upsert_source_fragment(
    db: Session,
    existing: dict[tuple[int, str, str], SourceFragment],
    doc: DocumentVersion,
    row: dict[str, Any],
    *,
    source_system: str,
    external_id: str,
) -> bool:
    key = (int(doc.id), source_system, external_id)
    fragment = existing.get(key)
    created = fragment is None
    if fragment is None:
        fragment = SourceFragment(document_version_id=int(doc.id), source_system=source_system, external_id=external_id)
        existing[key] = fragment
    page = row.get("page_number") or row.get("source_page_number") or row.get("output_page_number")
    bbox = row.get("bbox_normalized")
    bbox_pdf = row.get("bbox_pdf")
    page_width = _optional_float(row.get("page_width"))
    page_height = _optional_float(row.get("page_height"))
    if bbox is None and bbox_pdf is not None:
        bbox = _normalize_bbox_pdf(bbox_pdf, page_width, page_height)
    if bbox is None and source_system == "learning_page_index":
        bbox = [0.0, 0.0, 1.0, 1.0]
    if bbox_pdf is None and source_system == "learning_page_index" and page_width and page_height:
        bbox_pdf = [0.0, 0.0, page_width, page_height]
    metadata = dict(row)
    metadata["dataset_file_id"] = doc.dataset_file_id
    metadata["dataset_stage"] = doc.dataset_stage
    fragment.page = _optional_int(page)
    fragment.bbox = _normalize_bbox(bbox)
    fragment.bbox_pdf = _normalize_bbox_pdf_value(bbox_pdf)
    fragment.page_width = page_width
    fragment.page_height = page_height
    fragment.text = row.get("text")
    fragment.fragment_type = str(row.get("annotation_type") or ("page" if source_system == "learning_page_index" else "text")).lower()
    fragment.metadata_json = metadata
    fragment.extractor = row.get("source") or row.get("text_source") or "case10_dataset"
    fragment.confidence = _optional_float(row.get("confidence"))
    db.add(fragment)
    return created


def _param_from_catalog_row(
    row: dict[str, Any],
    *,
    matrix_row: dict[str, Any] | None,
    matrix: MatrixVersion,
    organization_id: int,
    project_id: int | None,
) -> Param:
    criticality = str(row.get("criticality") or "")
    parameter_id = row.get("parameter_id")
    scoring_code = str(row.get("parameter_code") or "").strip()
    parsed_parameter_id = _optional_int(parameter_id)
    matrix_code = str((matrix_row or {}).get("matrix_code") or "").strip()
    if not matrix_code and parsed_parameter_id:
        matrix_code = f"M-{parsed_parameter_id:03d}"
    if not matrix_code:
        matrix_code = scoring_code
    aliases = [value for value in (matrix_code, scoring_code) if value]
    priority = _review_priority_v11((matrix_row or {}).get("review_priority")) if matrix_row else None
    raw_sources = {
        "parameter_id": parameter_id,
        "matrix_row": row.get("matrix_row"),
        "matrix_code": matrix_code,
        "scoring_code": scoring_code,
        "aliases": aliases,
        "criticality": row.get("criticality"),
        "mapping_status": row.get("mapping_status"),
        "source_pd": row.get("source_pd"),
        "source_rd": row.get("source_rd"),
        "source_id": row.get("source_id"),
        "matrix_v1_1": matrix_row or {},
    }
    return Param(
        matrix_version_id=int(matrix.id),
        project_id=project_id,
        organization_id=int(organization_id),
        code=scoring_code,
        matrix_code=matrix_code,
        scoring_code=scoring_code,
        aliases_json=aliases,
        section=(matrix_row or {}).get("pd_section") or row.get("pd_section"),
        parameter_name=str((matrix_row or {}).get("parameter_name") or row.get("parameter_name") or "").strip(),
        unit=(matrix_row or {}).get("unit") or row.get("unit"),
        source_pd=_has_source((matrix_row or {}).get("source_pd") or row.get("source_pd")),
        source_rd=_has_source((matrix_row or {}).get("source_rd") or row.get("source_rd")),
        source_id=_has_source((matrix_row or {}).get("source_id") or row.get("source_id")),
        trigger_logic=_trigger_logic(row, matrix_row),
        review_priority=priority or ("HIGH" if "критическ" in criticality.lower() else "MEDIUM"),
        other_normative=json.dumps(raw_sources, ensure_ascii=False, sort_keys=True),
        data_type="number" if row.get("unit") else "string",
        regex_pattern=None,
        is_active=True,
    )


def _trigger_logic(row: dict[str, Any], matrix_row: dict[str, Any] | None = None) -> str:
    parts = [
        ("parameter_id", row.get("parameter_id")),
        ("matrix_row", row.get("matrix_row")),
        ("matrix_code", (matrix_row or {}).get("matrix_code")),
        ("scoring_code", row.get("parameter_code")),
        ("mapping_status", row.get("mapping_status")),
        ("trigger", (matrix_row or {}).get("trigger") or row.get("trigger")),
    ]
    return ";".join(f"{key}={str(value).replace(';', ',')}" for key, value in parts if value is not None)


def _review_priority_v11(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("HIGH") or "КРИТ" in text:
        return "HIGH"
    return "MEDIUM"


def _ensure_object(db: Session, row: dict[str, Any], *, project_id: int, organization_id: int) -> ConstructionObject:
    object_id = str(row.get("object_id") or "").strip()
    registry_id = dataset_object_registry_id(object_id, project_id, organization_id)
    existing = (
        db.query(ConstructionObject)
        .filter(
            ConstructionObject.id.in_((object_id, registry_id)),
            ConstructionObject.project_id == int(project_id),
            ConstructionObject.organization_id == int(organization_id),
        )
        .first()
    )
    if existing:
        return existing
    obj = ConstructionObject(
        id=registry_id,
        project_id=int(project_id),
        organization_id=int(organization_id),
        name=str(row.get("corpus") or object_id),
    )
    db.add(obj)
    db.flush()
    return obj


def dataset_object_registry_id(object_id: str, project_id: int, organization_id: int) -> str:
    return "DATASET-" + hashlib.sha256(f"{organization_id}:{project_id}:{object_id}".encode()).hexdigest()[:48]


def _documents_by_file_id(db: Session, *, project_id: int, organization_id: int) -> dict[tuple[str, str], DocumentVersion]:
    rows = (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.project_id == int(project_id),
            DocumentVersion.organization_id == int(organization_id),
            DocumentVersion.dataset_file_id.isnot(None),
        )
        .all()
    )
    return {(str(row.dataset_file_id), str(row.object_id or "")): row for row in rows}


def _doc_for_row(docs: dict[tuple[str, str], DocumentVersion], row: dict[str, Any]) -> DocumentVersion | None:
    return docs.get((str(row.get("file_id") or ""), str(row.get("object_id") or "")))


def _existing_fragments(db: Session, *, project_id: int, organization_id: int) -> dict[tuple[int, str, str], SourceFragment]:
    rows = (
        db.query(SourceFragment)
        .join(DocumentVersion, SourceFragment.document_version_id == DocumentVersion.id)
        .filter(
            DocumentVersion.project_id == int(project_id),
            DocumentVersion.organization_id == int(organization_id),
            SourceFragment.source_system.in_(("learning_page_index", "learning_annotation")),
        )
        .all()
    )
    return {
        (int(row.document_version_id), str(row.source_system), str(row.external_id or "")): row
        for row in rows
        if row.external_id
    }


def _document_counts(db: Session, *, project_id: int, organization_id: int) -> dict[str, int]:
    counts: Counter[str] = Counter()
    rows = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.project_id == int(project_id), DocumentVersion.organization_id == int(organization_id))
        .all()
    )
    for row in rows:
        counts[f"{row.object_id}:{row.dataset_stage or row.doc_stage or row.document_stage}"] += 1
    return dict(sorted(counts.items()))


def _gold_counts(db: Session, *, project_id: int, organization_id: int) -> dict[str, int]:
    counts: Counter[str] = Counter()
    rows = (
        db.query(GoldCheckFixture)
        .filter(GoldCheckFixture.project_id == int(project_id), GoldCheckFixture.organization_id == int(organization_id))
        .all()
    )
    for row in rows:
        counts[f"{row.object_id}:{row.parameter_code}:{row.violation_label}"] += 1
    return dict(sorted(counts.items()))


def _candidate_roots(dataset_root: str | Path | None) -> list[Path]:
    candidates: list[Path] = []
    if dataset_root:
        candidates.append(Path(dataset_root))
    env_root = os.getenv("CASE10_DATASET_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path.cwd())
    candidates.extend(Path(__file__).resolve().parents)

    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        key = str(resolved).lower()
        if key in seen or not resolved.is_dir():
            continue
        # Only search dataset roots, never an entire drive or container filesystem.
        if not ((resolved / "case_data").is_dir() or (resolved / "learning_data").is_dir()
                or resolved.name in {"case_data", "learning_data", "train_public_203", "test_hidden_213"}
                or dataset_root is not None and resolved == Path(dataset_root).resolve()):
            continue
        seen.add(key)
        out.append(resolved)
    return out[:1]


def _find_first(roots: list[Path], filename: str, *, prefer: tuple[str, ...] = (), avoid: tuple[str, ...] = ()) -> Path | None:
    matches: list[Path] = []
    for root in roots:
        try:
            search_roots = [root / "case_data" / "extracted", root / "learning_data" / "extracted"]
            if not any(path.is_dir() for path in search_roots):
                search_roots = [root]
            for search_root in search_roots:
                if search_root.is_dir():
                    matches.extend(path for path in search_root.rglob(filename) if not any(part in path.as_posix() for part in avoid))
        except OSError:
            continue
    if not matches:
        return None
    matches.sort(key=lambda path: (-sum(1 for part in prefer if part in path.as_posix()), len(path.as_posix()), path.as_posix()))
    return matches[0]


def _find_data_dir(roots: list[Path], marker: str) -> Path | None:
    for root in roots:
        direct = root / "learning_data" / "extracted" / marker / "data"
        if direct.exists():
            return direct
        if root.name == marker and (root / "data").exists():
            return root / "data"
    matches: list[Path] = []
    for root in roots:
        try:
            matches.extend(path for path in root.rglob("data") if marker in path.as_posix())
        except OSError:
            continue
    matches.sort(key=lambda path: (len(path.as_posix()), path.as_posix()))
    return matches[0] if matches else None


def _find_matrix_v11_xlsx(roots: list[Path]) -> Path | None:
    wanted = {
        "Матрица_параметров_редакция1.1.xlsx",
        "05_Матрица_132_параметра_существенная_редакция.xlsx",
        "ПРИЛОЖЕНИЕ 1. Матрица сравнения 132 параматеров.xlsx",
    }
    matches: list[Path] = []
    for root in roots:
        search_roots = [
            root / "tmp",
            root / "case_data" / "extracted",
            root / "learning_data" / "extracted",
        ]
        if root.name in {"tmp", "case_data", "learning_data"}:
            search_roots.append(root)
        for search_root in search_roots:
            if not search_root.is_dir():
                continue
            try:
                matches.extend(path for path in search_root.rglob("*.xlsx") if path.name in wanted)
            except OSError:
                continue
    if not matches:
        return None
    matches.sort(key=lambda path: (
        path.name != "Матрица_параметров_редакция1.1.xlsx",
        "tz_new_20260916" not in path.as_posix(),
        len(path.as_posix()),
        path.as_posix(),
    ))
    return matches[0]


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                yield payload


def _merge_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    keys = set(left) | set(right)
    return {key: int(left.get(key, 0)) + int(right.get(key, 0)) for key in sorted(keys)}


def _load_matrix_v11(path: Path) -> dict[int, dict[str, Any]]:
    rows = list(_xlsx_sheet_rows(path, "МАТРИЦА"))
    if not rows:
        raise HTTPException(status_code=400, detail=f"Matrix 1.1 sheet МАТРИЦА is empty: {_display_path(path)}")
    headers = [str(value or "").strip() for value in rows[0]]
    index = {name: position for position, name in enumerate(headers)}
    required = {
        "ID",
        "Код параметра",
        "Раздел ПД (ПП РФ № 87)",
        "Контролируемый параметр",
        "Ед. изм.",
        "Источник в ПД",
        "Источник в РД",
        "Источник в ИД",
        "Логика ИИ-связи (предварительный триггер)",
        "Приоритет экспертной проверки",
    }
    missing = sorted(required - set(index))
    if missing:
        raise HTTPException(status_code=400, detail=f"Matrix 1.1 is missing columns: {', '.join(missing)}")

    out: dict[int, dict[str, Any]] = {}
    for raw in rows[1:]:
        parameter_id = _optional_int(_cell_at(raw, index["ID"]))
        matrix_code = str(_cell_at(raw, index["Код параметра"]) or "").strip()
        if not parameter_id or not matrix_code:
            continue
        out[parameter_id] = {
            "parameter_id": parameter_id,
            "matrix_code": matrix_code,
            "pd_section": _cell_at(raw, index["Раздел ПД (ПП РФ № 87)"]),
            "parameter_name": _cell_at(raw, index["Контролируемый параметр"]),
            "unit": _cell_at(raw, index["Ед. изм."]),
            "source_pd": _cell_at(raw, index["Источник в ПД"]),
            "source_rd": _cell_at(raw, index["Источник в РД"]),
            "source_id": _cell_at(raw, index["Источник в ИД"]),
            "trigger": _cell_at(raw, index["Логика ИИ-связи (предварительный триггер)"]),
            "review_priority": _cell_at(raw, index["Приоритет экспертной проверки"]),
        }
    return out


def _generated_matrix_v11(catalog_rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    generated: dict[int, dict[str, Any]] = {}
    for row in catalog_rows:
        parameter_id = _optional_int(row.get("parameter_id"))
        if not parameter_id:
            continue
        generated[parameter_id] = {
            "parameter_id": parameter_id,
            "matrix_code": f"M-{parameter_id:03d}",
            "pd_section": row.get("pd_section"),
            "parameter_name": row.get("parameter_name"),
            "unit": row.get("unit"),
            "source_pd": row.get("source_pd"),
            "source_rd": row.get("source_rd"),
            "source_id": row.get("source_id"),
            "trigger": row.get("trigger"),
            "review_priority": "HIGH" if "критическ" in str(row.get("criticality") or "").lower() else "MEDIUM",
        }
    return generated


def _validate_matrix_v11(rows: dict[int, dict[str, Any]]) -> None:
    if len(rows) != 132:
        raise HTTPException(status_code=400, detail=f"Matrix 1.1 must contain 132 params, got {len(rows)}")
    expected_codes = {f"M-{index:03d}" for index in range(1, 133)}
    actual_codes = {str(row.get("matrix_code") or "") for row in rows.values()}
    missing_codes = sorted(expected_codes - actual_codes)
    if missing_codes:
        raise HTTPException(status_code=400, detail=f"Matrix 1.1 missing codes: {', '.join(missing_codes[:5])}")
    high = sum(1 for row in rows.values() if _review_priority_v11(row.get("review_priority")) == "HIGH")
    medium = sum(1 for row in rows.values() if _review_priority_v11(row.get("review_priority")) == "MEDIUM")
    if (high, medium) != (106, 26):
        raise HTTPException(status_code=400, detail=f"Matrix 1.1 priority split must be 106/26, got {high}/{medium}")


def _xlsx_sheet_rows(path: Path, sheet_name: str) -> Iterable[list[Any]]:
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    with zipfile.ZipFile(path) as archive:
        shared = _xlsx_shared_strings(archive, ns)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in relationships.findall("pkgrel:Relationship", ns)
            if "Id" in rel.attrib and "Target" in rel.attrib
        }
        sheet_path = None
        for sheet in workbook.findall("main:sheets/main:sheet", ns):
            if sheet.attrib.get("name") != sheet_name:
                continue
            rel_id = sheet.attrib.get(f"{{{ns['rel']}}}id")
            target = rel_targets.get(str(rel_id))
            if target:
                target = target.lstrip("/")
                sheet_path = target if target.startswith("xl/") else "xl/" + target
                break
        if not sheet_path:
            raise HTTPException(status_code=400, detail=f"Sheet {sheet_name} not found in {_display_path(path)}")
        sheet_xml = ET.fromstring(archive.read(sheet_path))
        for row in sheet_xml.findall("main:sheetData/main:row", ns):
            values: dict[int, Any] = {}
            max_col = 0
            for cell in row.findall("main:c", ns):
                col = _xlsx_column_index(str(cell.attrib.get("r") or "A1"))
                max_col = max(max_col, col)
                values[col] = _xlsx_cell_value(cell, shared, ns)
            yield [values.get(index) for index in range(1, max_col + 1)]


def _xlsx_shared_strings(archive: zipfile.ZipFile, ns: dict[str, str]) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for item in root.findall("main:si", ns):
        parts = [node.text or "" for node in item.findall(".//main:t", ns)]
        out.append("".join(parts))
    return out


def _xlsx_cell_value(cell: ET.Element, shared: list[str], ns: dict[str, str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", ns))
    value = cell.find("main:v", ns)
    if value is None or value.text is None:
        return None
    text_value = value.text
    if cell_type == "s":
        index = _optional_int(text_value)
        return shared[index] if index is not None and 0 <= index < len(shared) else text_value
    if cell_type == "b":
        return text_value == "1"
    return text_value


def _xlsx_column_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref.upper())
    if not letters:
        return 1
    index = 0
    for char in letters.group(1):
        index = index * 26 + ord(char) - ord("A") + 1
    return index


def _cell_at(row: list[Any], index: int) -> Any:
    return row[index] if 0 <= index < len(row) else None


def _filename_from_row(row: dict[str, Any]) -> str:
    rel = row.get("source_relative_path") or row.get("relative_path") or row.get("output_pdf") or row.get("file_id") or "document"
    return PurePosixPath(str(rel).replace("\\", "/")).name


def _document_code_from_path(row: dict[str, Any]) -> str | None:
    rel = row.get("source_relative_path") or row.get("relative_path") or row.get("output_pdf")
    if not rel:
        return row.get("file_id")
    return PurePosixPath(str(rel).replace("\\", "/")).stem[:255]


def _revision_from_path(row: dict[str, Any]) -> str | None:
    text = str(row.get("source_relative_path") or row.get("relative_path") or "")
    match = re.search(r"(?:rev|revision|изм\.?|ред\.?)\s*([A-Za-zА-Яа-яЁё0-9._-]+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _approval_status_for_row(row: dict[str, Any]) -> str:
    if str(row.get("distribution_status") or "").upper() == "EXCLUDE":
        return "EXCLUDED"
    if str(row.get("stage") or "").upper() == "UNKNOWN":
        return "UNKNOWN"
    return str(row.get("approval_status") or "UNKNOWN").upper()


def _has_source(value: object) -> bool:
    return bool(str(value or "").strip())


def _optional_float(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_bbox(value: object) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    x1, x2 = sorted((max(0.0, min(1.0, x1)), max(0.0, min(1.0, x2))))
    y1, y2 = sorted((max(0.0, min(1.0, y1)), max(0.0, min(1.0, y2))))
    return [x1, y1, x2, y2]


def _normalize_bbox_pdf(value: object, page_width: float | None, page_height: float | None) -> list[float] | None:
    bbox = _normalize_bbox_pdf_value(value)
    if not bbox or not page_width or not page_height:
        return None
    x1, y1, x2, y2 = bbox
    return _normalize_bbox([x1 / page_width, y1 / page_height, x2 / page_width, y2 / page_height])


def _normalize_bbox_pdf_value(value: object) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    return [x1, y1, x2, y2]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_files(paths: Iterable[Path | None]) -> str:
    h = hashlib.sha256()
    for path in paths:
        if not path:
            continue
        h.update(str(path.name).encode("utf-8"))
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
