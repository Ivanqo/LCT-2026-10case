from __future__ import annotations

import unittest

from rag_service.app.schemas import SourceChunk
from rag_service.app.source_selection import select_visible_sources


def _chunk(chunk_id: int, *, page_number: int, selected: bool = False) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        document_id=1,
        filename="doc.pdf",
        page_number=page_number,
        text=f"chunk {chunk_id}",
        selected=selected,
    )


class SourceSelectionTest(unittest.TestCase):
    def test_selected_source_does_not_pull_extra_pages(self) -> None:
        sources = [
            _chunk(1, page_number=5, selected=True),
            _chunk(2, page_number=6),
            _chunk(3, page_number=7),
        ]

        visible = select_visible_sources(sources)

        self.assertEqual([source.chunk_id for source in visible], [1])
        self.assertEqual([source.page_number for source in visible], [5])

    def test_falls_back_to_top_sources_when_nothing_is_selected(self) -> None:
        sources = [_chunk(i, page_number=i) for i in range(1, 6)]

        visible = select_visible_sources(sources)

        self.assertEqual([source.chunk_id for source in visible], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
