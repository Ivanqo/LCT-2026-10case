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
    SourceFragment,
)
from .entity_resolution import EntityResolver, ObservationDraft
from .normalization import normalize_alias


def sync_ifc_observations(
    db: Session,
    document_version: DocumentVersion,
    exported: list[dict[str, Any]],
) -> dict[str, int]:
    created_entities = 0
    matched_entities = 0
    observations_created = 0
    observations_updated = 0
    attributes_written = 0
    skipped = 0
    resolver = EntityResolver()

    for item in exported:
        guid = str(item.get("guid") or "").strip()
        ifc_type = str(item.get("ifc_type") or "").strip()
        external_id = str(item.get("external_id") or "").strip()
        if not guid or not ifc_type or not external_id:
            skipped += 1
            continue

        entity_type = _entity_type_from_ifc(ifc_type)
        attributes = _flatten_attributes(item)
        location = {
            key: value
            for key, value in {
                "storey_guid": item.get("storey_guid"),
                "container_guid": item.get("container_guid"),
            }.items()
            if value
        }
        raw_name = str(item.get("name") or ifc_type or guid)

        fragment = _upsert_ifc_fragment(db, document_version, item)
        draft = ObservationDraft(
            project_id=int(document_version.project_id),
            organization_id=int(document_version.organization_id),
            raw_name=raw_name,
            raw_mark=guid,
            entity_type=entity_type,
            location=location,
            attributes={name: value["value"] for name, value in attributes.items()},
        )
        match = resolver.best_match(db, draft)
        if match:
            entity = db.get(CanonicalEntity, match.canonical_entity_id)
            resolution_payload = match.to_dict()
            matched_entities += 1
        else:
            entity = CanonicalEntity(
                id=_next_entity_id(db, int(document_version.project_id), entity_type),
                project_id=int(document_version.project_id),
                organization_id=int(document_version.organization_id),
                entity_type=entity_type,
                canonical_name=raw_name,
                status="active",
            )
            db.add(entity)
            db.flush()
            resolution_payload = {
                "selected": entity.id,
                "confidence": 0.65,
                "reasons": [
                    {
                        "code": "ifc_new_entity",
                        "label": "new IFC canonical entity",
                        "contribution": 0.65,
                        "details": f"{ifc_type} {guid}",
                    }
                ],
            }
            created_entities += 1

        if not entity:
            skipped += 1
            continue

        _ensure_alias(db, entity.id, guid, "ifc_guid", 1.0, "ifc_index")
        if raw_name and raw_name != guid:
            _ensure_alias(db, entity.id, raw_name, "name", 0.8, "ifc_index")
        _ensure_alias(db, entity.id, ifc_type, "ifc_type", 0.7, "ifc_index")

        observation = (
            db.query(EntityObservation)
            .filter(EntityObservation.source_fragment_id == int(fragment.id), EntityObservation.raw_mark == guid)
            .first()
        )
        if observation:
            observation.canonical_entity_id = entity.id
            observation.document_version_id = int(document_version.id)
            observation.raw_name = raw_name
            observation.normalized_name = normalize_alias(raw_name)
            observation.location = location
            observation.confidence = resolution_payload.get("confidence", 0.65)
            observation.extractor = "ifc_index"
            observation.resolution_explanation = resolution_payload
            db.add(observation)
            db.flush()
            db.query(AttributeObservation).filter(AttributeObservation.entity_observation_id == observation.id).delete()
            observations_updated += 1
        else:
            observation = EntityObservation(
                canonical_entity_id=entity.id,
                document_version_id=int(document_version.id),
                source_fragment_id=int(fragment.id),
                raw_name=raw_name,
                raw_mark=guid,
                normalized_name=normalize_alias(raw_name),
                stage=document_version.document_stage,
                location=location,
                confidence=resolution_payload.get("confidence", 0.65),
                extractor="ifc_index",
                resolution_explanation=resolution_payload,
            )
            db.add(observation)
            db.flush()
            observations_created += 1

        for attr_name, attr in attributes.items():
            db.add(
                AttributeObservation(
                    entity_observation_id=int(observation.id),
                    source_fragment_id=int(fragment.id),
                    attribute_name=attr_name,
                    raw_value=str(attr["raw"]) if attr["raw"] is not None else None,
                    normalized_value=str(attr["value"]) if attr["value"] is not None else None,
                    normalized_numeric=attr["numeric"],
                    unit=attr["unit"],
                    confidence=0.75,
                    extractor="ifc_index",
                )
            )
            attributes_written += 1

    db.flush()
    return {
        "created_entities": created_entities,
        "matched_entities": matched_entities,
        "observations_created": observations_created,
        "observations_updated": observations_updated,
        "attributes_written": attributes_written,
        "skipped": skipped,
    }


def _upsert_ifc_fragment(db: Session, document_version: DocumentVersion, item: dict[str, Any]) -> SourceFragment:
    external_id = str(item.get("external_id") or "")
    fragment = (
        db.query(SourceFragment)
        .filter(
            SourceFragment.document_version_id == int(document_version.id),
            SourceFragment.source_system == "ifc",
            SourceFragment.external_id == external_id,
        )
        .first()
    )
    payload = {
        "guid": item.get("guid"),
        "ifc_type": item.get("ifc_type"),
        "storey_guid": item.get("storey_guid"),
        "container_guid": item.get("container_guid"),
        "attributes": item.get("attributes") if isinstance(item.get("attributes"), dict) else {},
        "quantities": item.get("quantities") if isinstance(item.get("quantities"), dict) else {},
        "metrics": item.get("metrics") if isinstance(item.get("metrics"), dict) else {},
    }
    if fragment:
        fragment.page = None
        fragment.bbox = []
        fragment.text = str(item.get("text") or "")
        fragment.fragment_type = "ifc"
        fragment.extractor = "ifc_index"
        fragment.confidence = 0.85
        fragment.metadata_json = payload
        db.add(fragment)
        db.flush()
        return fragment

    fragment = SourceFragment(
        document_version_id=int(document_version.id),
        page=None,
        bbox=[],
        text=str(item.get("text") or ""),
        fragment_type="ifc",
        source_system="ifc",
        external_id=external_id,
        metadata_json=payload,
        extractor="ifc_index",
        confidence=0.85,
    )
    db.add(fragment)
    db.flush()
    return fragment


def _flatten_attributes(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, value in (item.get("attributes") or {}).items():
        out[f"ifc_property.{name}"] = _attribute_payload(value, None)
    for name, payload in (item.get("quantities") or {}).items():
        if isinstance(payload, dict):
            out[f"ifc_quantity.{name}"] = _attribute_payload(payload.get("value"), payload.get("unit"))
        else:
            out[f"ifc_quantity.{name}"] = _attribute_payload(payload, None)
    for name, value in (item.get("metrics") or {}).items():
        if name in {"bbox_min", "bbox_max", "source"}:
            continue
        out[f"ifc_metric.{name}"] = _attribute_payload(value, None)
    return out


def _attribute_payload(value: Any, unit: str | None) -> dict[str, Any]:
    numeric = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return {"raw": value, "value": value, "numeric": numeric, "unit": unit}


def _entity_type_from_ifc(ifc_type: str) -> str:
    raw = re.sub(r"^ifc", "", str(ifc_type or ""), flags=re.IGNORECASE).strip().lower()
    mapping = {
        "wall": "wall",
        "wallstandardcase": "wall",
        "door": "door",
        "window": "window",
        "slab": "slab",
        "beam": "beam",
        "column": "column",
        "space": "room",
        "buildingstorey": "floor",
    }
    return mapping.get(raw, raw or "ifc_element")


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
        "window": "WINDOW",
        "slab": "SLAB",
        "beam": "BEAM",
        "column": "COLUMN",
    }.get(entity_type, re.sub(r"[^A-Z0-9]+", "", entity_type.upper())[:8] or "ENT")
    rows = (
        db.query(CanonicalEntity.id)
        .filter(CanonicalEntity.project_id == int(project_id), CanonicalEntity.id.like(f"{prefix}-%"))
        .all()
    )
    max_num = 0
    for (raw_id,) in rows:
        match = re.search(r"-(\d+)$", raw_id or "")
        if match:
            max_num = max(max_num, int(match.group(1)))
    return f"{prefix}-{max_num + 1:06d}"

