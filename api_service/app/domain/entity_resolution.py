from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import AttributeObservation, CanonicalEntity, EntityAlias, EntityObservation
from .normalization import location_similarity, normalize_alias, normalize_text, text_similarity, value_similarity


@dataclass
class ResolutionReason:
    code: str
    label: str
    contribution: float
    details: str


@dataclass
class EntityResolutionCandidate:
    canonical_entity_id: str
    canonical_name: str
    entity_type: str
    confidence: float
    reasons: list[ResolutionReason] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_entity_id": self.canonical_entity_id,
            "canonical_name": self.canonical_name,
            "entity_type": self.entity_type,
            "confidence": round(self.confidence, 4),
            "reasons": [
                {
                    "code": reason.code,
                    "label": reason.label,
                    "contribution": round(reason.contribution, 4),
                    "details": reason.details,
                }
                for reason in self.reasons
            ],
        }


@dataclass
class ObservationDraft:
    project_id: int
    organization_id: int
    raw_name: str | None = None
    raw_mark: str | None = None
    entity_type: str | None = None
    location: dict[str, Any] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


class EntityResolver:
    """Explainable deterministic resolver for Observation -> CanonicalEntity."""

    threshold = 0.4

    def resolve(self, db: Session, draft: ObservationDraft, limit: int = 5) -> list[EntityResolutionCandidate]:
        entities = (
            db.query(CanonicalEntity)
            .filter(
                CanonicalEntity.project_id == int(draft.project_id),
                CanonicalEntity.organization_id == int(draft.organization_id),
                CanonicalEntity.status == "active",
            )
            .all()
        )

        candidates: list[EntityResolutionCandidate] = []
        for entity in entities:
            scored = self._score_entity(db, entity, draft)
            if scored.confidence > 0:
                candidates.append(scored)

        candidates.sort(key=lambda item: item.confidence, reverse=True)
        return candidates[:limit]

    def best_match(self, db: Session, draft: ObservationDraft) -> EntityResolutionCandidate | None:
        candidates = self.resolve(db, draft, limit=1)
        if not candidates:
            return None
        best = candidates[0]
        if best.confidence < self.threshold:
            return None
        return best

    def _score_entity(self, db: Session, entity: CanonicalEntity, draft: ObservationDraft) -> EntityResolutionCandidate:
        reasons: list[ResolutionReason] = []
        aliases = (
            db.query(EntityAlias)
            .filter(EntityAlias.canonical_entity_id == entity.id, EntityAlias.status == "active")
            .all()
        )

        raw_values = [v for v in [draft.raw_mark, draft.raw_name] if v]
        normalized_values = {normalize_alias(v) for v in raw_values if normalize_alias(v)}
        alias_norms = {alias.normalized_alias for alias in aliases if alias.normalized_alias}
        alias_labels = {alias.normalized_alias: alias.alias for alias in aliases if alias.normalized_alias}

        if normalized_values & alias_norms:
            matched = sorted(normalized_values & alias_norms)[0]
            reasons.append(
                ResolutionReason(
                    code="alias_exact",
                    label="alias exact match",
                    contribution=0.5,
                    details=f"{alias_labels.get(matched, matched)} == {matched}",
                )
            )

        if not any(r.code == "alias_exact" for r in reasons):
            for observed in normalized_values:
                for known in alias_norms:
                    if observed and known and (observed in known or known in observed):
                        reasons.append(
                            ResolutionReason(
                                code="alias_normalized",
                                label="normalized alias match",
                                contribution=0.35,
                                details=f"{observed} ~ {known}",
                            )
                        )
                        break
                if any(r.code == "alias_normalized" for r in reasons):
                    break

        name_scores: list[tuple[float, str]] = []
        for value in raw_values:
            name_scores.append((text_similarity(value, entity.canonical_name), entity.canonical_name))
            for alias in aliases:
                name_scores.append((text_similarity(value, alias.alias), alias.alias))
        if name_scores:
            name_score, matched_name = max(name_scores, key=lambda item: item[0])
            if name_score >= 0.55:
                contribution = 0.25 * name_score
                reasons.append(
                    ResolutionReason(
                        code="fuzzy_name",
                        label="fuzzy name similarity",
                        contribution=contribution,
                        details=f"{draft.raw_name or draft.raw_mark or ''} ~ {matched_name}",
                    )
                )

        if draft.entity_type and normalize_text(draft.entity_type) == normalize_text(entity.entity_type):
            reasons.append(
                ResolutionReason(
                    code="entity_type",
                    label="same entity type",
                    contribution=0.1,
                    details=entity.entity_type,
                )
            )

        latest_observation = (
            db.query(EntityObservation)
            .filter(EntityObservation.canonical_entity_id == entity.id)
            .order_by(EntityObservation.created_at.desc(), EntityObservation.id.desc())
            .first()
        )
        loc_score = location_similarity(draft.location, latest_observation.location if latest_observation else None)
        if loc_score > 0:
            reasons.append(
                ResolutionReason(
                    code="location",
                    label="location similarity",
                    contribution=0.2 * loc_score,
                    details=f"{round(loc_score * 100)}% location fields matched",
                )
            )

        attr_score = self._attribute_similarity(db, entity.id, draft.attributes)
        if attr_score > 0:
            reasons.append(
                ResolutionReason(
                    code="attributes",
                    label="attribute similarity",
                    contribution=0.2 * attr_score,
                    details=f"{round(attr_score * 100)}% comparable attributes matched",
                )
            )

        total = min(1.0, sum(reason.contribution for reason in reasons))
        return EntityResolutionCandidate(
            canonical_entity_id=entity.id,
            canonical_name=entity.canonical_name,
            entity_type=entity.entity_type,
            confidence=total,
            reasons=reasons,
        )

    def _attribute_similarity(self, db: Session, entity_id: str, attributes: dict[str, Any]) -> float:
        if not attributes:
            return 0.0
        rows = (
            db.query(AttributeObservation)
            .join(EntityObservation, AttributeObservation.entity_observation_id == EntityObservation.id)
            .filter(EntityObservation.canonical_entity_id == entity_id)
            .all()
        )
        latest_by_name: dict[str, Any] = {}
        for row in rows:
            latest_by_name[row.attribute_name] = row.normalized_numeric
            if latest_by_name[row.attribute_name] is None:
                latest_by_name[row.attribute_name] = row.normalized_value or row.raw_value

        comparable = 0
        matched = 0.0
        for name, value in attributes.items():
            if name not in latest_by_name:
                continue
            comparable += 1
            matched += value_similarity(value, latest_by_name[name])
        if not comparable:
            return 0.0
        return matched / comparable

