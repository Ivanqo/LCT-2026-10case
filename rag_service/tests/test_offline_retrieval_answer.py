from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from rag_service.app.llm_structured import ask_structured_answer


class OfflineRetrievalAnswerTest(unittest.TestCase):
    def test_returns_retrieved_context_without_opening_network_connections(self) -> None:
        chunks = [
            {
                "chunk_id": 7,
                "filename": "doc.pdf",
                "page_number": 5,
                "text": "В проектной документации указана отметка 120.000.",
            }
        ]

        with (
            patch.object(socket, "create_connection", side_effect=AssertionError("network access is disabled")),
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("network access is disabled")),
        ):
            answer, chunk_ids, asset_ids, raw = ask_structured_answer(
                question="Какая отметка указана?",
                chunks=chunks,
                assets=[],
            )

        self.assertIn("120.000", answer)
        self.assertIn("doc.pdf, лист 5", answer)
        self.assertEqual(chunk_ids, [7])
        self.assertEqual(asset_ids, [])
        self.assertEqual(raw, "")


if __name__ == "__main__":
    unittest.main()
