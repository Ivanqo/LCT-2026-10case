"""Count unique registry entries and their matched package files by document stage."""
from __future__ import annotations

from collections import Counter
from typing import Iterable, Any


def registry_stage_counts(documents: Iterable[Any]) -> tuple[bool, Counter[str], Counter[str]]:
    expected: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    has_summary = False
    rows = list(documents)

    def entry_key(item: dict[str, Any]) -> tuple[str, str, str, str] | None:
        stage = str(item.get("doc_stage") or "").strip().upper()
        if stage not in {"PD", "RD", "ID"}:
            return None
        file_id = str(item.get("file_id") or "").strip()
        sha256 = str(item.get("sha256") or "").strip().lower()
        file_name = str(item.get("file_name") or "").strip().replace("\\", "/")
        if not (file_id or sha256 or file_name):
            return None
        # Keep distinct rows with a duplicated file ID separate. For an older
        # or sparse row the remaining columns are simply empty on both the
        # package summary and its matching registry entry.
        return stage, file_id, sha256, file_name

    for doc in rows:
        metadata = getattr(doc, "dataset_metadata", None) or {}
        manifest = metadata.get("document_manifest", {}) if isinstance(metadata, dict) else {}
        if not isinstance(manifest, dict):
            continue
        summary = manifest.get("registry_manifest_summary")
        if isinstance(summary, list):
            has_summary = True
            for item in summary:
                if isinstance(item, dict):
                    key = entry_key(item)
                    if key:
                        expected[key] = item

    loaded: set[tuple[str, str, str, str]] = set()
    if expected:
        expected_keys = set(expected)
        for doc in rows:
            if str(getattr(doc, "dataset_split", "") or "") != "BATCH_PACKAGE":
                continue
            metadata = getattr(doc, "dataset_metadata", None) or {}
            manifest = metadata.get("document_manifest", {}) if isinstance(metadata, dict) else {}
            if not isinstance(manifest, dict) or not isinstance(manifest.get("registry"), dict):
                continue
            key = entry_key(manifest["registry"])
            if key in expected_keys:
                loaded.add(key)

    expected_by_stage = Counter(key[0] for key in expected)
    loaded_by_stage = Counter(key[0] for key in loaded)
    return has_summary, expected_by_stage, loaded_by_stage
