from __future__ import annotations

import unittest
import sys
import types
from typing import Any
from unittest.mock import patch

try:
    import requests as _requests  # noqa: F401
except ModuleNotFoundError:
    _requests_stub = types.ModuleType("requests")

    class _StubHTTPError(Exception):
        pass

    class _StubSession:
        pass

    class _StubResponse:
        pass

    _requests_stub.HTTPError = _StubHTTPError
    _requests_stub.Session = _StubSession
    _requests_stub.Response = _StubResponse
    sys.modules["requests"] = _requests_stub

from rag_service.app import llm_structured
from shared.llm import QwenProxyClient


class _FakeQwenClient:
    instances: list["_FakeQwenClient"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.chat_calls: list[dict[str, Any]] = []
        self.chat_completions_calls: list[dict[str, Any]] = []
        self.instances.append(self)

    def chat(self, **kwargs: Any) -> tuple[dict[str, Any], float]:
        del kwargs
        raise AssertionError("chat should only be a legacy fallback")

    def chat_completions(self, **kwargs: Any) -> tuple[dict[str, Any], float]:
        self.chat_completions_calls.append(kwargs)
        content = '{"answer": "Direct HTTP response with enough detail.", "chunk_ids": [7], "asset_ids": []}'
        return {"choices": [{"message": {"content": content}}]}, 0.01


class LlmDirectApiTest(unittest.TestCase):
    def setUp(self) -> None:
        _FakeQwenClient.instances = []

    def test_structured_answer_uses_direct_chat_completions_request_first(self) -> None:
        chunks = [
            {
                "chunk_id": 7,
                "document_id": 1,
                "filename": "doc.pdf",
                "page_number": 5,
                "text": "source text",
                "score": 0.9,
            }
        ]

        with patch.object(llm_structured, "QwenProxyClient", _FakeQwenClient):
            answer, chunk_ids, asset_ids, raw = llm_structured.ask_structured_answer(
                question="What is on the page?",
                chunks=chunks,
                assets=[],
                system_prompt="system",
                model="qwen-test",
            )

        client = _FakeQwenClient.instances[-1]
        self.assertEqual(len(client.chat_completions_calls), 1)
        self.assertEqual(client.chat_completions_calls[0]["model"], "qwen-test")
        self.assertEqual(client.chat_completions_calls[0]["extra"], {"temperature": 0.1, "stream": False})
        self.assertEqual(client.chat_calls, [])
        self.assertEqual(chunk_ids, [7])
        self.assertEqual(asset_ids, [])
        self.assertIn("Direct HTTP response", answer)
        self.assertIn("chunk_ids", raw)


class _FakeResponse:
    status_code = 200
    text = (
        '{"details":"{\\"success\\":false,\\"request_id\\":\\"93bbcf79-f94b-4894-94db-bcded8b70d33\\",'
        '\\"data\\":{\\"code\\":\\"Not_Found\\",\\"details\\":\\"Model not found\\"}}",'
        '"chatId":"514e23ea-5446-4486-9a03-2b1f61346871"}'
    )

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "details": (
                '{"success":false,"request_id":"93bbcf79-f94b-4894-94db-bcded8b70d33",'
                '"data":{"code":"Not_Found","details":"Model not found"}}'
            ),
            "chatId": "514e23ea-5446-4486-9a03-2b1f61346871",
        }


class _FakeSession:
    def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        del args, kwargs
        return _FakeResponse()


class QwenProxyContractTest(unittest.TestCase):
    def test_chat_rejects_success_false_details_payload(self) -> None:
        client = QwenProxyClient(base_url="http://proxy.test/api", session=_FakeSession())

        with self.assertRaisesRegex(RuntimeError, "Not_Found: Model not found"):
            client.chat(
                model="qwen3.7-max",
                messages=[{"role": "user", "content": "hello"}],
                extra={"stream": False},
            )


if __name__ == "__main__":
    unittest.main()
