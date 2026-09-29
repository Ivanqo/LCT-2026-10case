from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.api import routes_chat


class _RagClient:
    async def ask(self, **kwargs):
        del kwargs
        return {"answer": "Найдено в документе.", "sources": [{"filename": "doc.pdf", "page": 2}]}


class _IfcClient:
    async def get_context(self, **kwargs):
        del kwargs
        return {"context_text": "Контекст IFC.", "items": []}


class ChatContextFallbackTests(unittest.TestCase):
    def test_combines_retrieved_context_without_external_generation(self) -> None:
        with patch.object(routes_chat, "RagClient", _RagClient), patch.object(routes_chat, "IfcClient", _IfcClient):
            result = asyncio.run(
                routes_chat._ask_rag_and_ifc(
                    organization_id=1,
                    project_id=2,
                    question="Вопрос по объекту",
                    top_k=4,
                )
            )

        self.assertIn("Найдено в документе.", result["answer"])
        self.assertIn("Контекст IFC.", result["answer"])
        self.assertEqual(result["retrieval_mode"], "documents+ifc")
        self.assertFalse(hasattr(routes_chat, "_call_llm_for_merged_answer"))


if __name__ == "__main__":
    unittest.main()
