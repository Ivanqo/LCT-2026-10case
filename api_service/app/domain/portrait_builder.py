from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..db.models import (
    AttributeObservation,
    CanonicalEntity,
    ChangeEvent,
    ChangeIssue,
    DocumentVersion,
    EntityAlias,
    EntityObservation,
    EntityRelation,
    SourceFragment,
)


STAGE_ORDER = {
    "project": 10,
    "working": 20,
    "as_built": 30,
    "ifc": 40,
    "unknown": 90,
}


def stage_sort_key(stage: str | None) -> tuple[int, str]:
    raw = stage or "unknown"
    return (STAGE_ORDER.get(raw, 80), raw)


def source_ref(fragment: SourceFragment | None, doc: DocumentVersion | None) -> dict[str, Any] | None:
    if not fragment and not doc:
        return None
    return {
        "document_version_id": doc.id if doc else None,
        "filename": doc.filename if doc else None,
        "stage": doc.document_stage if doc else None,
        "version": doc.version if doc else None,
        "revision": doc.revision if doc else None,
        "page": fragment.page if fragment else None,
        "bbox": fragment.bbox if fragment else None,
        "fragment_id": fragment.id if fragment else None,
        "fragment_type": fragment.fragment_type if fragment else None,
        "extractor": fragment.extractor if fragment else None,
        "confidence": fragment.confidence if fragment else None,
    }


class PortraitBuilder:
    def build(self, db: Session, entity_id: str, organization_id: int) -> dict[str, Any]:
        entity = (
            db.query(CanonicalEntity)
            .filter(CanonicalEntity.id == entity_id, CanonicalEntity.organization_id == int(organization_id))
            .first()
        )
        if not entity:
            raise HTTPException(status_code=404, detail="Canonical entity not found")

        aliases = (
            db.query(EntityAlias)
            .filter(EntityAlias.canonical_entity_id == entity.id)
            .order_by(EntityAlias.alias_type.asc(), EntityAlias.alias.asc())
            .all()
        )

        observations = (
            db.query(EntityObservation, DocumentVersion, SourceFragment)
            .join(DocumentVersion, EntityObservation.document_version_id == DocumentVersion.id)
            .outerjoin(SourceFragment, EntityObservation.source_fragment_id == SourceFragment.id)
            .filter(EntityObservation.canonical_entity_id == entity.id)
            .all()
        )
        observations = sorted(
            observations,
            key=lambda row: (
                stage_sort_key(row[1].document_stage),
                row[1].version or "",
                row[1].revision or "",
                row[0].id,
            ),
        )

        attrs = (
            db.query(AttributeObservation, EntityObservation, DocumentVersion, SourceFragment)
            .join(EntityObservation, AttributeObservation.entity_observation_id == EntityObservation.id)
            .join(DocumentVersion, EntityObservation.document_version_id == DocumentVersion.id)
            .outerjoin(SourceFragment, AttributeObservation.source_fragment_id == SourceFragment.id)
            .filter(EntityObservation.canonical_entity_id == entity.id)
            .all()
        )
        attrs = sorted(
            attrs,
            key=lambda row: (
                row[0].attribute_name,
                stage_sort_key(row[2].document_stage),
                row[2].version or "",
                row[2].revision or "",
                row[0].id,
            ),
        )

        attributes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        timeline: dict[str, dict[str, Any]] = {}
        evidence_by_fragment: dict[int, dict[str, Any]] = {}
        for attr, obs, doc, fragment in attrs:
            value = attr.normalized_numeric if attr.normalized_numeric is not None else attr.normalized_value or attr.raw_value
            item = {
                "id": attr.id,
                "observation_id": obs.id,
                "stage": doc.document_stage,
                "version": doc.version,
                "revision": doc.revision,
                "attribute": attr.attribute_name,
                "raw_value": attr.raw_value,
                "value": value,
                "unit": attr.unit,
                "confidence": attr.confidence,
                "source": source_ref(fragment, doc),
            }
            attributes[attr.attribute_name].append(item)
            stage_key = doc.document_stage or obs.stage or "unknown"
            timeline.setdefault(
                stage_key,
                {
                    "stage": stage_key,
                    "version": doc.version,
                    "revision": doc.revision,
                    "attributes": {},
                    "observation_ids": [],
                },
            )
            timeline[stage_key]["attributes"][attr.attribute_name] = item
            if obs.id not in timeline[stage_key]["observation_ids"]:
                timeline[stage_key]["observation_ids"].append(obs.id)
            if fragment:
                evidence_by_fragment[int(fragment.id)] = source_ref(fragment, doc) | {"text": fragment.text}

        locations = []
        seen_locations = set()
        observation_items = []
        stages = set()
        versions = []
        for obs, doc, fragment in observations:
            stages.add(doc.document_stage)
            versions.append(
                {
                    "document_version_id": doc.id,
                    "filename": doc.filename,
                    "stage": doc.document_stage,
                    "version": doc.version,
                    "revision": doc.revision,
                }
            )
            if obs.location:
                key = repr(sorted(obs.location.items()))
                if key not in seen_locations:
                    seen_locations.add(key)
                    locations.append(obs.location)
            observation_items.append(
                {
                    "id": obs.id,
                    "raw_name": obs.raw_name,
                    "raw_mark": obs.raw_mark,
                    "stage": doc.document_stage,
                    "version": doc.version,
                    "revision": doc.revision,
                    "location": obs.location,
                    "confidence": obs.confidence,
                    "extractor": obs.extractor,
                    "resolution_explanation": obs.resolution_explanation,
                    "source": source_ref(fragment, doc),
                }
            )
            if fragment:
                evidence_by_fragment[int(fragment.id)] = source_ref(fragment, doc) | {"text": fragment.text}

        relation_rows = (
            db.query(EntityRelation)
            .filter((EntityRelation.from_entity_id == entity.id) | (EntityRelation.to_entity_id == entity.id))
            .all()
        )
        relations = []
        for relation in relation_rows:
            other_id = relation.to_entity_id if relation.from_entity_id == entity.id else relation.from_entity_id
            other = db.get(CanonicalEntity, other_id)
            relations.append(
                {
                    "id": relation.id,
                    "direction": "outgoing" if relation.from_entity_id == entity.id else "incoming",
                    "relation_type": relation.relation_type,
                    "entity_id": other_id,
                    "entity_name": other.canonical_name if other else other_id,
                    "confidence": relation.confidence,
                }
            )

        change_rows = (
            db.query(ChangeEvent, ChangeIssue)
            .outerjoin(ChangeIssue, ChangeIssue.change_event_id == ChangeEvent.id)
            .filter(ChangeEvent.canonical_entity_id == entity.id)
            .order_by(ChangeEvent.created_at.asc(), ChangeEvent.id.asc())
            .all()
        )
        changes = []
        max_risk = 0.0
        max_severity = "NONE"
        issues = []
        for event, issue in change_rows:
            issue_payload = None
            if issue:
                max_risk = max(max_risk, float(issue.risk_score or 0))
                max_severity = _max_severity(max_severity, issue.severity)
                issue_payload = {
                    "id": issue.id,
                    "risk_score": issue.risk_score,
                    "severity": issue.severity,
                    "explanation": issue.explanation,
                    "status": issue.status,
                }
                issues.append(issue_payload | {"change_event_id": event.id})
            changes.append(
                {
                    "id": event.id,
                    "attribute": event.attribute,
                    "change_type": event.change_type,
                    "delta": event.delta,
                    "confidence": event.confidence,
                    "issue": issue_payload,
                }
            )

        return {
            "identity": {
                "id": entity.id,
                "project_id": entity.project_id,
                "entity_type": entity.entity_type,
                "canonical_name": entity.canonical_name,
                "status": entity.status,
                "created_at": entity.created_at,
            },
            "aliases": [
                {
                    "id": alias.id,
                    "alias": alias.alias,
                    "alias_type": alias.alias_type,
                    "confidence": alias.confidence,
                    "source": alias.source,
                    "status": alias.status,
                }
                for alias in aliases
            ],
            "locations": locations,
            "observations": observation_items,
            "attributes": dict(attributes),
            "relations": relations,
            "stages": sorted(stages, key=stage_sort_key),
            "versions": versions,
            "timeline": sorted(timeline.values(), key=lambda item: stage_sort_key(item["stage"])),
            "evidence": list(evidence_by_fragment.values()),
            "changes": changes,
            "issues": issues,
            "risk": {
                "score": max_risk,
                "severity": max_severity,
            },
        }


def _max_severity(left: str | None, right: str | None) -> str:
    rank = {"NONE": 0, "LOW": 1, "WARNING": 2, "NEEDS_REVIEW": 3, "CRITICAL": 4}
    left_key = left or "NONE"
    right_key = right or "NONE"
    return right_key if rank.get(right_key, 0) > rank.get(left_key, 0) else left_key

