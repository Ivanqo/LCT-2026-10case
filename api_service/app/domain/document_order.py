"""One content-derived total order over documents, shared by every place that has to pick "the" source of a value.

`DocumentVersion.id` is assigned in import order, so a ranking that breaks ties on it makes the chosen value depend on
the order of the package's file list (final-round Step 1, hypothesis H2). The file id and the content hash are
properties of the data; the id only breaks a tie between two rows that are otherwise indistinguishable. This is the
same key the live tagger already plans its scans by (`live_candidate_tagger._document_sort_key`)."""
from __future__ import annotations

from typing import Any


def document_order_key(document: Any, fallback_id: int | None = None) -> tuple[str, str, int]:
    if document is None:
        return ("", "", int(fallback_id or 0))
    return (
        str(getattr(document, "dataset_file_id", None) or ""),
        str(getattr(document, "file_hash", None) or getattr(document, "content_hash", None) or ""),
        int(getattr(document, "id", None) or fallback_id or 0),
    )
