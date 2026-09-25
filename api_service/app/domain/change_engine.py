from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import AttributeObservation, ChangeEvent, ChangeIssue, DocumentVersion, EntityObservation
from .normalization import comparable_value
from .portrait_builder import stage_sort_key


CHANGE_ADDED = "ADDED"
CHANGE_REMOVED = "REMOVED"
CHANGE_VALUE_CHANGED = "VALUE_CHANGED"
CHANGE_VALUE_MISSING = "VALUE_MISSING"
CHANGE_TYPE_CHANGED = "TYPE_CHANGED"
CHANGE_CONFLICT = "CONFLICT"


@dataclass
class ComputedChange:
    canonical_entity_id: str
    attribute: str
    change_type: str
    previous_observation_id: int | None
    next_observation_id: int | None
    delta: dict[str, Any]
    confidence: float


class ChangeEngine:
    def compute_for_entity(self, db: Session, canonical_entity_id: str) -> list[ComputedChange]:
        rows = (
            db.query(AttributeObservation, EntityObservation, DocumentVersion)
            .join(EntityObservation, AttributeObservation.entity_observation_id == EntityObservation.id)
            .join(DocumentVersion, EntityObservation.document_version_id == DocumentVersion.id)
            .filter(EntityObservation.canonical_entity_id == canonical_entity_id)
            .all()
        )
        by_observation: dict[int, dict[str, Any]] = {}
        for attr, obs, doc in rows:
            obs_bucket = by_observation.setdefault(
                obs.id,
                {
                    "observation": obs,
                    "document": doc,
                    "attributes": {},
                    "raw_attributes": {},
                },
            )
            obs_bucket["attributes"][attr.attribute_name] = attr
            obs_bucket["raw_attributes"].setdefault(attr.attribute_name, []).append(attr)

        ordered = sorted(
            by_observation.values(),
            key=lambda item: (
                stage_sort_key(item["document"].document_stage),
                item["document"].version or "",
                item["document"].revision or "",
                item["observation"].id,
            ),
        )

        changes: list[ComputedChange] = []
        changes.extend(self._conflicts(canonical_entity_id, ordered))
        for prev, nxt in zip(ordered, ordered[1:]):
            changes.extend(self._compare_observations(canonical_entity_id, prev, nxt))
        return changes

    def refresh_entity_changes(self, db: Session, canonical_entity_id: str) -> list[ChangeEvent]:
        computed = self.compute_for_entity(db, canonical_entity_id)
        events: list[ChangeEvent] = []
        for item in computed:
            existing = (
                db.query(ChangeEvent)
                .filter(
                    ChangeEvent.canonical_entity_id == item.canonical_entity_id,
                    ChangeEvent.attribute == item.attribute,
                    ChangeEvent.change_type == item.change_type,
                    ChangeEvent.previous_observation_id == item.previous_observation_id,
                    ChangeEvent.next_observation_id == item.next_observation_id,
                )
                .first()
            )
            if existing:
                events.append(existing)
                if not existing.issue:
                    self._ensure_issue(db, existing)
                continue

            event = ChangeEvent(
                canonical_entity_id=item.canonical_entity_id,
                attribute=item.attribute,
                previous_observation_id=item.previous_observation_id,
                next_observation_id=item.next_observation_id,
                change_type=item.change_type,
                delta=item.delta,
                confidence=item.confidence,
            )
            db.add(event)
            db.flush()
            self._ensure_issue(db, event)
            events.append(event)
        db.flush()
        return events

    def _compare_observations(self, canonical_entity_id: str, prev: dict[str, Any], nxt: dict[str, Any]) -> list[ComputedChange]:
        prev_attrs: dict[str, AttributeObservation] = prev["attributes"]
        next_attrs: dict[str, AttributeObservation] = nxt["attributes"]
        names = sorted(set(prev_attrs.keys()) | set(next_attrs.keys()))
        changes: list[ComputedChange] = []
        for name in names:
            prev_attr = prev_attrs.get(name)
            next_attr = next_attrs.get(name)
            if prev_attr and not next_attr:
                changes.append(
                    ComputedChange(
                        canonical_entity_id=canonical_entity_id,
                        attribute=name,
                        change_type=CHANGE_VALUE_MISSING,
                        previous_observation_id=prev_attr.id,
                        next_observation_id=None,
                        delta=self._delta(prev_attr, None, prev, nxt),
                        confidence=float(prev_attr.confidence or 0.7),
                    )
                )
                continue
            if next_attr and not prev_attr:
                changes.append(
                    ComputedChange(
                        canonical_entity_id=canonical_entity_id,
                        attribute=name,
                        change_type=CHANGE_ADDED,
                        previous_observation_id=None,
                        next_observation_id=next_attr.id,
                        delta=self._delta(None, next_attr, prev, nxt),
                        confidence=float(next_attr.confidence or 0.7),
                    )
                )
                continue
            if not prev_attr or not next_attr:
                continue

            prev_value = comparable_value(_value(prev_attr))
            next_value = comparable_value(_value(next_attr))
            if prev_value == next_value:
                continue
            change_type = CHANGE_TYPE_CHANGED if name in {"entity_type", "type"} else CHANGE_VALUE_CHANGED
            confidence = min(float(prev_attr.confidence or 0.75), float(next_attr.confidence or 0.75))
            changes.append(
                ComputedChange(
                    canonical_entity_id=canonical_entity_id,
                    attribute=name,
                    change_type=change_type,
                    previous_observation_id=prev_attr.id,
                    next_observation_id=next_attr.id,
                    delta=self._delta(prev_attr, next_attr, prev, nxt),
                    confidence=confidence,
                )
            )
        return changes

    def _conflicts(self, canonical_entity_id: str, ordered: list[dict[str, Any]]) -> list[ComputedChange]:
        changes: list[ComputedChange] = []
        for item in ordered:
            for name, attrs in item["raw_attributes"].items():
                values = {comparable_value(_value(attr)) for attr in attrs}
                if len(values) <= 1:
                    continue
                changes.append(
                    ComputedChange(
                        canonical_entity_id=canonical_entity_id,
                        attribute=name,
                        change_type=CHANGE_CONFLICT,
                        previous_observation_id=attrs[0].id,
                        next_observation_id=attrs[-1].id,
                        delta={
                            "stage": item["document"].document_stage,
                            "values": [_value(attr) for attr in attrs],
                        },
                        confidence=min(float(attr.confidence or 0.7) for attr in attrs),
                    )
                )
        return changes

    def _delta(
        self,
        prev_attr: AttributeObservation | None,
        next_attr: AttributeObservation | None,
        prev: dict[str, Any],
        nxt: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "from": _attribute_payload(prev_attr, prev.get("document")),
            "to": _attribute_payload(next_attr, nxt.get("document")),
        }

    def _ensure_issue(self, db: Session, event: ChangeEvent) -> ChangeIssue:
        if event.issue:
            return event.issue
        risk_score, severity, explanation = score_change(event)
        issue = ChangeIssue(
            change_event_id=event.id,
            risk_score=risk_score,
            severity=severity,
            explanation=explanation,
            status="New",
        )
        db.add(issue)
        db.flush()
        return issue


def score_change(event: ChangeEvent) -> tuple[float, str, str]:
    delta = event.delta or {}
    from_value = ((delta.get("from") or {}).get("value") or "")
    to_value = ((delta.get("to") or {}).get("value") or "")
    attribute = (event.attribute or "").lower()

    if event.change_type == CHANGE_CONFLICT:
        return 72.0, "NEEDS_REVIEW", "В одном наблюдении найдены конфликтующие значения параметра."
    if event.change_type in {CHANGE_VALUE_MISSING, CHANGE_REMOVED}:
        return 58.0, "NEEDS_REVIEW", "Значение параметра пропало в следующем наблюдении и требует проверки инспектором."
    if event.change_type == CHANGE_ADDED:
        return 35.0, "LOW", "Параметр появился в следующем наблюдении; это нужно учитывать в истории объекта."

    if "fire" in attribute or "огне" in attribute or "ei" in str(from_value).lower() or "ei" in str(to_value).lower():
        prev_ei = _extract_ei(from_value)
        next_ei = _extract_ei(to_value)
        if prev_ei is not None and next_ei is not None and next_ei < prev_ei:
            return 88.0, "CRITICAL", f"Огнестойкость снижена {from_value} -> {to_value}."
        return 64.0, "WARNING", f"Изменена противопожарная характеристика {from_value} -> {to_value}."

    if "thickness" in attribute or "толщ" in attribute:
        prev_num = _as_float(from_value)
        next_num = _as_float(to_value)
        if prev_num and next_num:
            drop = (prev_num - next_num) / prev_num
            if drop >= 0.25:
                return 82.0, "CRITICAL", f"Толщина уменьшена более чем на 25%: {from_value} -> {to_value}."
            if drop > 0:
                return 62.0, "WARNING", f"Толщина уменьшена: {from_value} -> {to_value}."
        return 55.0, "WARNING", f"Толщина изменилась: {from_value} -> {to_value}."

    if "material" in attribute or "материал" in attribute:
        return 60.0, "WARNING", f"Материал изменился: {from_value} -> {to_value}."

    return 45.0, "NEEDS_REVIEW", f"Значение изменилось: {from_value} -> {to_value}."


def _value(attr: AttributeObservation | None) -> Any:
    if not attr:
        return None
    if attr.normalized_numeric is not None:
        return attr.normalized_numeric
    return attr.normalized_value or attr.raw_value


def _attribute_payload(attr: AttributeObservation | None, doc: DocumentVersion | None) -> dict[str, Any] | None:
    if not attr:
        return None
    return {
        "attribute_observation_id": attr.id,
        "stage": doc.document_stage if doc else None,
        "version": doc.version if doc else None,
        "revision": doc.revision if doc else None,
        "raw_value": attr.raw_value,
        "value": _value(attr),
        "unit": attr.unit,
        "confidence": attr.confidence,
    }


def _extract_ei(value: Any) -> int | None:
    match = re.search(r"EI\s*(\d+)", str(value or ""), flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[\.,]\d+)?", str(value or ""))
    if not match:
        return None
    return float(match.group(0).replace(",", "."))

