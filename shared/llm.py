#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm.py — готовый к запуску клиент для Qwen-proxy API (http://localhost:3264/api)

Основано на присланных примерах:
- POST /api/chat: {messages:[...], model} или {message, model, chatId}
- POST /api/chats: {name} -> {chatId} и GET /api/chats/{chatId}
- POST /api/files/upload (multipart/form-data)

Что умеет:
- Загружать файлы через POST /files/upload (multipart/form-data)
- Формировать payload сообщений с вложениями в формате, который ожидает фронт (как в streamlit/chat.js):
    messages: [{ role:"user", content:"...", files:[{...file_obj...}, ...] }]
- Создавать/использовать chatId и продолжать диалог
- Опционально подтягивать историю чата через GET /chats/{chatId}
- Вести агрегированную статистику usage/latency по чату (если прокси возвращает usage)

Зависимости:
    pip install requests

Примеры:
    python llm.py --message "Привет! Что ты умеешь?" --model qwen3.7-max
    python llm.py --new-chat "Тестовый чат" --message "Расскажи про async/await"
    python llm.py --chat-id <CHAT_ID> --message "Приведи пример"
    python llm.py --file ./doc1.pdf --file ./img.png --message "Проанализируй эти файлы"
    python llm.py --interactive --new-chat "REPL чат"

Переменные окружения (опционально):
    QWEN_PROXY_BASE_URL  базовый URL, напр. http://localhost:3264/api
    QWEN_MODEL           модель по умолчанию, напр. qwen3.7-max
    QWEN_TIMEOUT         таймаут в секундах (по умолчанию 120)
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

DEFAULT_BASE_URL = os.getenv("QWEN_PROXY_BASE_URL", "http://localhost:3264/api").rstrip("/")
DEFAULT_MODEL = os.getenv("QWEN_MODEL", "qwen3.7-max")
DEFAULT_TIMEOUT = float(os.getenv("QWEN_TIMEOUT", "360"))

DEFAULT_SYSTEM_PROMPT = (
    "Ты — профессиональный технический ассистент. "
    "Отвечай по делу, структурировано и на русском."
)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @staticmethod
    def from_payload(payload: Any) -> "Usage":
        if not isinstance(payload, dict):
            return Usage()
        pt = int(payload.get("prompt_tokens") or payload.get("promptTokens") or 0)
        ct = int(payload.get("completion_tokens") or payload.get("completionTokens") or 0)
        tt = int(payload.get("total_tokens") or payload.get("totalTokens") or (pt + ct) or 0)
        return Usage(prompt_tokens=pt, completion_tokens=ct, total_tokens=tt)

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


@dataclass
class ChatAccounting:
    chat_id: Optional[str] = None
    model: Optional[str] = None
    requests: int = 0
    usage: Usage = field(default_factory=Usage)
    total_latency_s: float = 0.0

    def add_request(self, usage: Usage, latency_s: float, model: Optional[str]) -> None:
        self.requests += 1
        self.usage.add(usage)
        self.total_latency_s += float(latency_s or 0.0)
        if model and not self.model:
            self.model = model

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chatId": self.chat_id,
            "model": self.model,
            "requests": self.requests,
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            },
            "total_latency_s": round(self.total_latency_s, 3),
            "avg_latency_s": round(self.total_latency_s / self.requests, 3) if self.requests else 0.0,
        }


def _has_answer_shape(payload: Dict[str, Any]) -> bool:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        return True
    for key in ("answer", "text", "content", "response"):
        value = payload.get(key)
        if value not in (None, "", []):
            return True
    return False


def _try_parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _format_error_value(value: Any) -> str:
    value = _try_parse_json_string(value)
    if not isinstance(value, dict):
        return str(value)

    data = value.get("data") if isinstance(value.get("data"), dict) else None
    target = data or value
    code = target.get("code") or target.get("type") or value.get("code") or value.get("type")
    message = (
        target.get("message")
        or target.get("details")
        or target.get("detail")
        or target.get("error")
        or value.get("message")
        or value.get("details")
        or value.get("detail")
        or value.get("error")
    )
    message = _try_parse_json_string(message)
    if isinstance(message, dict):
        message = _format_error_value(message)

    if code and message:
        text = f"{code}: {message}"
    elif message:
        text = str(message)
    else:
        text = json.dumps(value, ensure_ascii=False)

    request_id = value.get("request_id") or value.get("requestId")
    if request_id:
        text = f"{text} (request_id={request_id})"
    return text


def _extract_error_message(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    if payload.get("error"):
        return _format_error_value(payload.get("error"))

    if payload.get("success") is False:
        return _format_error_value(payload)

    data = payload.get("data")
    if isinstance(data, dict) and not _has_answer_shape(payload) and (data.get("code") or data.get("details") or data.get("error")):
        return _format_error_value(payload)

    details = payload.get("details")
    if details and not _has_answer_shape(payload):
        parsed = _try_parse_json_string(details)
        if isinstance(parsed, dict):
            nested = _extract_error_message(parsed)
            return nested or _format_error_value(parsed)
        return str(details)

    return None


def _ensure_no_error_payload(payload: Any) -> None:
    message = _extract_error_message(payload)
    if message:
        raise RuntimeError(f"LLM proxy returned error payload: {message}")


class QwenProxyClient:
    """
    Мини-клиент Qwen-proxy.

    Важно:
      * /files/upload — multipart/form-data, возвращает JSON с данными файла (url/size/type и т.п.)
      * /chat         — принимает messages[] и поддерживает поле files в user-сообщении
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http = session or requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            text = ""
            try:
                text = resp.text
            except Exception:
                pass
            raise requests.HTTPError(f"{e}\nResponse body:\n{text}") from None

    def upload_file(self, file_path: str) -> Dict[str, Any]:
        """
        Загружает файл в proxy и возвращает RAW JSON ответа (как есть).
        Используйте build_file_obj(...) чтобы получить структуру, пригодную для messages[].files.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        filename = os.path.basename(file_path)
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "application/octet-stream"

        with open(file_path, "rb") as f:
            files = {"file": (filename, f, mime)}
            t0 = time.perf_counter()
            resp = self.http.post(self._url("/files/upload"), files=files, timeout=self.timeout)
            latency = time.perf_counter() - t0

        self._raise_for_status(resp)
        data = resp.json()
        data["_latency_s"] = latency
        data["_local_path"] = file_path
        return data

    @staticmethod
    def build_file_obj(upload_resp: Dict[str, Any], *, filename_fallback: Optional[str] = None) -> Dict[str, Any]:
        """
        Приводит ответ /files/upload к формату file-объекта, который ожидает фронт (chat.js/streamlit_app.py):
            {
              "type": "file",
              "file": {"filename": "...", "meta": {"name": "...", "size": N, "content_type": "..."}},
              "url": "...",
              "name": "...",
              "size": N,
              "file_type": "..."
            }

        Поддерживает разные формы ответа прокси (на практике встречаются):
          * {"file": {"url":..., "size":..., "type":...}}
          * {"url":..., "size":..., "type":..., "filename":...}
        """
        if not isinstance(upload_resp, dict):
            raise ValueError("upload_resp must be a dict")

        local_path = upload_resp.get("_local_path") or ""
        filename = (
            upload_resp.get("filename")
            or upload_resp.get("fileName")
            or (upload_resp.get("file") or {}).get("name")
            or (os.path.basename(local_path) if local_path else None)
            or filename_fallback
            or "file"
        )

        file_node = upload_resp.get("file") if isinstance(upload_resp.get("file"), dict) else {}
        url = upload_resp.get("url") or upload_resp.get("file_url") or upload_resp.get("fileUrl") or file_node.get("url")
        size = (
            upload_resp.get("filesize")
            or upload_resp.get("size")
            or file_node.get("size")
            or file_node.get("filesize")
            or None
        )
        ftype = upload_resp.get("type") or upload_resp.get("file_type") or upload_resp.get("fileType") or file_node.get("type")

        # size should be int when possible
        try:
            size_int = int(size) if size is not None else None
        except Exception:
            size_int = None

        meta = {"name": filename}
        if size_int is not None:
            meta["size"] = size_int
        if ftype:
            meta["content_type"] = ftype

        out = {
            "type": "file",
            "file": {
                "filename": filename,
                "meta": meta,
            },
            "url": url,
            "name": filename,
            "size": size_int,
            "file_type": ftype,
        }

        # Clean None-ish keys to keep payload tidy
        if out["size"] is None:
            out.pop("size", None)
            out["file"]["meta"].pop("size", None)
        if out.get("url") is None:
            out.pop("url", None)
        if out.get("file_type") is None:
            out.pop("file_type", None)
            out["file"]["meta"].pop("content_type", None)

        return out

    def create_chat(self, name: str, model: Optional[str] = None) -> str:
        payload = {"name": name}
        if model:
            payload["model"] = model
        resp = self.http.post(self._url("/chats"), json=payload, timeout=self.timeout)
        self._raise_for_status(resp)
        data = resp.json()
        _ensure_no_error_payload(data)
        chat_id = data.get("chatId") or data.get("id")
        if not chat_id:
            raise RuntimeError(f"Unexpected response: {data}")
        return chat_id

    def chat_completions(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], float]:
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if extra:
            payload.update(extra)

        t0 = time.perf_counter()
        resp = self.http.post(self._url("/chat/completions"), json=payload, timeout=self.timeout)
        latency = time.perf_counter() - t0
        self._raise_for_status(resp)
        data = resp.json()
        _ensure_no_error_payload(data)
        return data, latency

    def get_chat_history(self, chat_id: str) -> Dict[str, Any]:
        resp = self.http.get(self._url(f"/chats/{chat_id}"), timeout=self.timeout)
        self._raise_for_status(resp)
        return resp.json()

    def chat(
        self,
        *,
        model: str,
        message: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        chat_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], float]:
        if not messages and not message:
            raise ValueError("Either 'message' or 'messages' must be provided")

        payload: Dict[str, Any] = {"model": model}
        if messages is not None:
            payload["messages"] = messages
        else:
            payload["message"] = message

        if chat_id:
            payload["chatId"] = chat_id
        if extra:
            payload.update(extra)

        t0 = time.perf_counter()
        resp = self.http.post(self._url("/chat"), json=payload, timeout=self.timeout)
        latency = time.perf_counter() - t0
        try:
            self._raise_for_status(resp)
            data = resp.json()
            _ensure_no_error_payload(data)
            return data, latency
        except requests.HTTPError as exc:
            body = ""
            try:
                body = resp.text or ""
            except Exception:
                body = ""
            lowered = body.lower()
            should_retry_with_chat = (
                messages is not None
                and not chat_id
                and ("ошибка при создании чата" in lowered or "error creating chat" in lowered or resp.status_code >= 500)
            )
            if should_retry_with_chat:
                fallback_chat_id = self.create_chat(name="RAG request", model=model)
                payload["chatId"] = fallback_chat_id
                t1 = time.perf_counter()
                resp2 = self.http.post(self._url("/chat"), json=payload, timeout=self.timeout)
                latency += time.perf_counter() - t1
                self._raise_for_status(resp2)
                data = resp2.json()
                _ensure_no_error_payload(data)
                data.setdefault("chatId", fallback_chat_id)
                return data, latency

            should_retry_completions = messages is not None and not chat_id
            if should_retry_completions:
                data, latency2 = self.chat_completions(model=model, messages=messages, extra=extra)
                return data, latency + latency2
            raise exc


def _build_messages(
    system_prompt: str,
    user_text: str,
    file_objs: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Формирует messages[] для /chat.

    Если file_objs не пустой — добавляет их в user-сообщение как поле "files".
    """
    final_user = (user_text or "").strip()

    user_msg: Dict[str, Any] = {"role": "user", "content": final_user}
    if file_objs:
        user_msg["files"] = list(file_objs)

    return [
        {"role": "system", "content": (system_prompt or DEFAULT_SYSTEM_PROMPT).strip()},
        user_msg,
    ]


def _extract_assistant_text(chat_response: Dict[str, Any]) -> str:
    try:
        return chat_response["choices"][0]["message"]["content"]
    except Exception:
        return (
            chat_response.get("answer")
            or chat_response.get("content")
            or chat_response.get("message")
            or str(chat_response)
        )


def _extract_chat_id(chat_response: Dict[str, Any]) -> Optional[str]:
    return chat_response.get("chatId") or chat_response.get("chat_id") or chat_response.get("id")


def _extract_model(chat_response: Dict[str, Any]) -> Optional[str]:
    return chat_response.get("model") or chat_response.get("requested_model")


def _extract_usage(chat_response: Dict[str, Any]) -> Usage:
    if isinstance(chat_response, dict) and "usage" in chat_response:
        return Usage.from_payload(chat_response.get("usage"))
    try:
        return Usage.from_payload(chat_response["choices"][0].get("usage"))
    except Exception:
        return Usage()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Qwen-proxy client: chat + file upload + attachments + accounting")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Base URL, default: {DEFAULT_BASE_URL}")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Model, default: {DEFAULT_MODEL}")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout, default: {DEFAULT_TIMEOUT}s")

    p.add_argument("--new-chat", default=None, help="Create new chat with this name and use it")
    p.add_argument("--chat-id", default=None, help="Use existing chatId")

    p.add_argument("--system", default=DEFAULT_SYSTEM_PROMPT, help="System prompt text")
    p.add_argument("--system-file", default=None, help="Path to file with system prompt (utf-8)")

    p.add_argument("--file", action="append", default=[], help="File path to upload & attach (can be used multiple times)")
    p.add_argument("--message", default=None, help="User message to send")
    p.add_argument("--interactive", action="store_true", help="Interactive REPL mode (stdin)")

    p.add_argument("--print-history", action="store_true", help="Print chat history after sending message(s)")
    p.add_argument("--raw", action="store_true", help="Print raw JSON response from /chat")
    return p.parse_args(argv)


def _load_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    system_prompt = args.system
    if args.system_file:
        system_prompt = _load_text_file(args.system_file)

    client = QwenProxyClient(base_url=args.base_url, timeout=args.timeout)

    # 1) upload files -> build file objects
    file_objs: List[Dict[str, Any]] = []
    for fp in args.file or []:
        print(f"[upload] {fp}")
        try:
            upload_resp = client.upload_file(fp)
        except Exception as e:
            print(f"  -> FAILED: {e}", file=sys.stderr)
            return 2

        try:
            file_obj = client.build_file_obj(upload_resp)
        except Exception as e:
            print(f"  -> FAILED to normalize upload response: {e}", file=sys.stderr)
            return 2

        file_objs.append(file_obj)
        url = file_obj.get("url")
        print(f"  -> ok; name={file_obj.get('name')}, url={url}")

    # 2) chatId strategy
    chat_id = args.chat_id
    if args.new_chat:
        try:
            chat_id = client.create_chat(args.new_chat)
            print(f"[chat] created: chatId={chat_id}")
        except Exception as e:
            print(f"[chat] create FAILED: {e}", file=sys.stderr)
            return 2

    accounting = ChatAccounting(chat_id=chat_id, model=args.model)

    def send_one(user_text: str) -> None:
        nonlocal chat_id, accounting
        messages = _build_messages(system_prompt, user_text, file_objs)
        if chat_id:
            resp_json, latency_s = client.chat(model=args.model, messages=messages, chat_id=chat_id)
        else:
            resp_json, latency_s = client.chat_completions(model=args.model, messages=messages, extra={"stream": False})

        assistant = _extract_assistant_text(resp_json)
        new_chat_id = _extract_chat_id(resp_json) or chat_id
        used_model = _extract_model(resp_json) or args.model
        usage = _extract_usage(resp_json)

        chat_id = new_chat_id
        accounting.chat_id = chat_id
        accounting.add_request(usage=usage, latency_s=latency_s, model=used_model)

        print("\n=== ASSISTANT ===")
        print(str(assistant).strip())
        print("=== META ===")
        print(f"chatId: {chat_id}")
        print(f"model:  {used_model}")
        if usage.total_tokens or usage.prompt_tokens or usage.completion_tokens:
            print(f"usage:  prompt={usage.prompt_tokens}, completion={usage.completion_tokens}, total={usage.total_tokens}")
        print(f"latency_s: {latency_s:.3f}")

        if args.raw:
            import json as _json
            print("\n--- RAW JSON ---")
            print(_json.dumps(resp_json, ensure_ascii=False, indent=2))

    if args.message:
        send_one(args.message)

    if args.interactive:
        print("\n[interactive] Введите сообщения. Пустая строка или Ctrl-D — выход.\n")
        while True:
            try:
                user_text = input("> ").strip()
            except EOFError:
                break
            if not user_text:
                break
            send_one(user_text)

    import json as _json
    print("\n=== ACCOUNTING SUMMARY ===")
    print(_json.dumps(accounting.to_dict(), ensure_ascii=False, indent=2))

    if args.print_history and chat_id:
        try:
            hist = client.get_chat_history(chat_id)
            print("\n=== CHAT HISTORY (raw) ===")
            print(_json.dumps(hist, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"[history] FAILED: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
