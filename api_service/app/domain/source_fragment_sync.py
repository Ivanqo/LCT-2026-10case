from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..db.models import DocumentVersion, SourceFragment


def sync_exported_source_fragments(
    db: Session,
    document_version: DocumentVersion,
    exported: list[dict[str, Any]],
) -> dict[str, int]:
    created = 0
    updated = 0
    skipped = 0

    for item in exported:
        external_id = str(item.get("external_id") or "").strip()
        if not external_id:
            skipped += 1
            continue
        source_system = str(item.get("source_system") or document_version.source_type or "external").strip() or "external"
        text_value = str(item.get("text") or "")
        bbox_value = item.get("bbox") if isinstance(item.get("bbox"), list) else []

        fragment = (
            db.query(SourceFragment)
            .filter(
                SourceFragment.document_version_id == int(document_version.id),
                SourceFragment.source_system == source_system,
                SourceFragment.external_id == external_id,
            )
            .first()
        )
        if fragment:
            fragment.page = _optional_int(item.get("page"))
            fragment.bbox = bbox_value
            fragment.text = text_value
            fragment.fragment_type = str(item.get("fragment_type") or "text")
            fragment.extractor = str(item.get("extractor") or "external_export")
            fragment.confidence = _optional_float(item.get("confidence"))
            fragment.metadata_json = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            db.add(fragment)
            updated += 1
            continue

        db.add(
            SourceFragment(
                document_version_id=int(document_version.id),
                page=_optional_int(item.get("page")),
                bbox=bbox_value,
                text=text_value,
                fragment_type=str(item.get("fragment_type") or "text"),
                source_system=source_system,
                external_id=external_id,
                metadata_json=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                extractor=str(item.get("extractor") or "external_export"),
                confidence=_optional_float(item.get("confidence")),
            )
        )
        created += 1

    db.flush()
    return {"created": created, "updated": updated, "skipped": skipped}


def _optional_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None

