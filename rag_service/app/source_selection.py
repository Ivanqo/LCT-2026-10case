from __future__ import annotations

from .schemas import SourceChunk


MAX_VISIBLE_SOURCES = 8
FALLBACK_VISIBLE_SOURCES = 3


def select_visible_sources(sources: list[SourceChunk]) -> list[SourceChunk]:
    selected = [source for source in sources if source.selected]
    if selected:
        return selected[:MAX_VISIBLE_SOURCES]
    return sources[: min(FALLBACK_VISIBLE_SOURCES, len(sources))]
