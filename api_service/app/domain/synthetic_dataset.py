from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import (
    AttributeObservation,
    CanonicalEntity,
    DocumentVersion,
    EntityAlias,
    EntityObservation,
    EntityRelation,
    SourceFragment,
)
from .change_engine import ChangeEngine
from .entity_resolution import EntityResolver, ObservationDraft
from .normalization import normalize_alias


SYNTHETIC_DATASET_ID = "case10-synthetic-v1"


SYNTHETIC_WALL = [
    {
        "stage": "project",
        "stage_label": "П",
        "filename": "SYNTHETIC_P_AR.pdf",
        "raw_name": "Wall A",
        "raw_mark": "A",
        "location": {"building": "1", "floor": "3", "axes": "A-B / 4-7"},
        "attributes": {
            "thickness": {"raw": "250 mm", "value": "250", "numeric": 250.0, "unit": "mm"},
            "fire_resistance": {"raw": "EI60", "value": "EI60", "numeric": None, "unit": None},
            "material": {"raw": "D500", "value": "D500", "numeric": None, "unit": None},
        },
    },
    {
        "stage": "working",
        "stage_label": "Р",
        "filename": "SYNTHETIC_R_AR.pdf",
        "raw_name": "Wall A1",
        "raw_mark": "A1",
        "location": {"building": "1", "floor": "3", "axes": "A-B / 4-7"},
        "attributes": {
            "thickness": {"raw": "200 mm", "value": "200", "numeric": 200.0, "unit": "mm"},
            "fire_resistance": {"raw": "EI60", "value": "EI60", "numeric": None, "unit": None},
            "material": {"raw": "D500", "value": "D500", "numeric": None, "unit": None},
        },
    },
    {
        "stage": "as_built",
        "stage_label": "И",
        "filename": "SYNTHETIC_I_AR.pdf",
        "raw_name": "Wall ST-01",
        "raw_mark": "ST-01",
        "location": {"building": "1", "floor": "3", "axes": "A-B / 4-7"},
        "attributes": {
            "thickness": {"raw": "180 mm", "value": "180", "numeric": 180.0, "unit": "mm"},
            "fire_resistance": {"raw": "EI30", "value": "EI30", "numeric": None, "unit": None},
            "material": {"raw": "Brick", "value": "Brick", "numeric": None, "unit": None},
        },
    },
]


def ensure_synthetic_case10_dataset(db: Session, project_id: int, organization_id: int) -> dict[str, Any]:
    existing = (
        db.query(CanonicalEntity)
        .filter(
            CanonicalEntity.project_id == int(project_id),
            CanonicalEntity.organization_id == int(organization_id),
            CanonicalEntity.canonical_name == "Synthetic Wall A",
        )
        .first()
    )
    if existing:
        ChangeEngine().refresh_entity_changes(db, existing.id)
        db.commit()
        return {"created": False, "canonical_entity_id": existing.id, "dataset_id": SYNTHETIC_DATASET_ID}

    resolver = EntityResolver()
    wall_entity: CanonicalEntity | None = None
    first_fragment: SourceFragment | None = None

    for item in SYNTHETIC_WALL:
        doc = _create_document_version(db, project_id, organization_id, item)
        fragment = _create_fragment(db, doc, item)
        if first_fragment is None:
            first_fragment = fragment

        attributes_for_resolution = {
            name: attr["numeric"] if attr["numeric"] is not None else attr["value"]
            for name, attr in item["attributes"].items()
        }
        draft = ObservationDraft(
            project_id=int(project_id),
            organization_id=int(organization_id),
            raw_name=item["raw_name"],
            raw_mark=item["raw_mark"],
            entity_type="wall",
            location=item["location"],
            attributes=attributes_for_resolution,
        )

        if wall_entity is None:
            wall_entity = CanonicalEntity(
                id=_next_entity_id(db, project_id, "wall"),
                project_id=int(project_id),
                organization_id=int(organization_id),
                entity_type="wall",
                canonical_name="Synthetic Wall A",
                status="active",
            )
            db.add(wall_entity)
            db.flush()
            _ensure_alias(db, wall_entity.id, item["raw_name"], "name", 1.0, "synthetic_seed")
            _ensure_alias(db, wall_entity.id, item["raw_mark"], "mark", 1.0, "synthetic_seed")
            resolution_payload = {
                "selected": wall_entity.id,
                "confidence": 1.0,
                "reasons": [{"code": "synthetic_seed", "label": "initial canonical entity", "contribution": 1.0}],
            }
        else:
            match = resolver.best_match(db, draft)
            if not match:
                raise RuntimeError("Synthetic dataset resolver failed to link wall observations")
            resolution_payload = match.to_dict()
            _ensure_alias(db, match.canonical_entity_id, item["raw_name"], "name", match.confidence, "entity_resolution")
            _ensure_alias(db, match.canonical_entity_id, item["raw_mark"], "mark", match.confidence, "entity_resolution")

        observation = EntityObservation(
            canonical_entity_id=wall_entity.id,
            document_version_id=doc.id,
            source_fragment_id=fragment.id,
            raw_name=item["raw_name"],
            raw_mark=item["raw_mark"],
            normalized_name=normalize_alias(item["raw_name"]),
            stage=item["stage"],
            location=item["location"],
            confidence=resolution_payload.get("confidence", 1.0),
            extractor="synthetic_case10_fixture",
            resolution_explanation=resolution_payload,
        )
        db.add(observation)
        db.flush()
        for attr_name, attr in item["attributes"].items():
            db.add(
                AttributeObservation(
                    entity_observation_id=observation.id,
                    source_fragment_id=fragment.id,
                    attribute_name=attr_name,
                    raw_value=attr["raw"],
                    normalized_value=attr["value"],
                    normalized_numeric=attr["numeric"],
                    unit=attr["unit"],
                    confidence=0.99,
                    extractor="synthetic_case10_fixture",
                )
            )
        db.flush()

    if wall_entity is None:
        raise RuntimeError("Synthetic dataset did not create a wall entity")

    floor_entity = CanonicalEntity(
        id=_next_entity_id(db, project_id, "floor"),
        project_id=int(project_id),
        organization_id=int(organization_id),
        entity_type="floor",
        canonical_name="Synthetic Floor 3",
        status="active",
    )
    db.add(floor_entity)
    db.flush()
    _ensure_alias(db, floor_entity.id, "Floor 3", "name", 1.0, "synthetic_seed")

    relation = EntityRelation(
        from_entity_id=wall_entity.id,
        to_entity_id=floor_entity.id,
        relation_type="LOCATED_IN",
        confidence=0.95,
        source_fragment_id=first_fragment.id if first_fragment else None,
    )
    db.add(relation)
    db.flush()

    ChangeEngine().refresh_entity_changes(db, wall_entity.id)
    db.commit()
    return {"created": True, "canonical_entity_id": wall_entity.id, "dataset_id": SYNTHETIC_DATASET_ID}


def _create_document_version(db: Session, project_id: int, organization_id: int, item: dict[str, Any]) -> DocumentVersion:
    doc = DocumentVersion(
        project_id=int(project_id),
        organization_id=int(organization_id),
        source_type="synthetic",
        source_document_id=None,
        object_id=f"OBJ-{int(project_id):06d}",
        filename=item["filename"],
        doc_stage=item["stage"],
        document_stage=item["stage"],
        discipline="AR",
        document_code=f"SYNTHETIC-{item['stage_label']}-AR",
        version="v1",
        revision="1",
        approval_status="APPROVED",
        content_hash=f"{SYNTHETIC_DATASET_ID}:{item['stage']}",
        file_hash=f"{SYNTHETIC_DATASET_ID}:{item['stage']}",
        file_path=f"synthetic://{SYNTHETIC_DATASET_ID}/{item['stage']}",
    )
    db.add(doc)
    db.flush()
    return doc


def _create_fragment(db: Session, doc: DocumentVersion, item: dict[str, Any]) -> SourceFragment:
    attrs_text = "; ".join(f"{name} = {payload['raw']}" for name, payload in item["attributes"].items())
    fragment = SourceFragment(
        document_version_id=doc.id,
        page=1,
        bbox=[0.11, 0.32, 0.43, 0.37],
        text=f"{item['stage_label']}: {item['raw_name']} ({item['raw_mark']}), {attrs_text}",
        fragment_type="text",
        source_system="synthetic",
        external_id=f"{SYNTHETIC_DATASET_ID}:{item['stage']}:wall",
        metadata_json={"dataset_id": SYNTHETIC_DATASET_ID, "stage": item["stage"]},
        extractor="synthetic_case10_fixture",
        confidence=0.99,
    )
    db.add(fragment)
    db.flush()
    return fragment


def _ensure_alias(
    db: Session,
    canonical_entity_id: str,
    alias: str,
    alias_type: str,
    confidence: float,
    source: str,
) -> EntityAlias:
    normalized = normalize_alias(alias)
    existing = (
        db.query(EntityAlias)
        .filter(
            EntityAlias.canonical_entity_id == canonical_entity_id,
            EntityAlias.normalized_alias == normalized,
            EntityAlias.alias_type == alias_type,
        )
        .first()
    )
    if existing:
        return existing
    row = EntityAlias(
        canonical_entity_id=canonical_entity_id,
        alias=alias,
        normalized_alias=normalized,
        alias_type=alias_type,
        confidence=confidence,
        source=source,
        status="active",
    )
    db.add(row)
    db.flush()
    return row


def _next_entity_id(db: Session, project_id: int, entity_type: str) -> str:
    prefix = {
        "wall": "WALL",
        "floor": "FLOOR",
        "door": "DOOR",
        "room": "ROOM",
    }.get(entity_type, re.sub(r"[^A-Z0-9]+", "", entity_type.upper())[:8] or "ENT")
    project_prefix = f"{prefix}-P{int(project_id):06d}"
    rows = (
        db.query(CanonicalEntity.id)
        .filter(CanonicalEntity.project_id == int(project_id), CanonicalEntity.id.like(f"{project_prefix}-%"))
        .all()
    )
    max_num = 0
    for (raw_id,) in rows:
        match = re.search(r"-(\d+)$", raw_id or "")
        if match:
            max_num = max(max_num, int(match.group(1)))
    return f"{project_prefix}-{max_num + 1:06d}"
