from __future__ import annotations

from typing import List, Optional, Sequence, Dict, Any
import os

from .llm import QwenProxyClient, DEFAULT_BASE_URL, DEFAULT_TIMEOUT


def _should_try_chat_endpoint(exc: Exception) -> bool:
    text = str(exc).lower()
    legacy_markers = (
        "404 client error",
        "405 client error",
        "404 not found",
        "405 method not allowed",
        "cannot post",
        "method not allowed",
    )
    return any(marker in text for marker in legacy_markers)


def build_prompt(question: str, context_chunks: List[str]) -> str:
    ctx = "\n\n---\n\n".join(context_chunks)
    return (
        "Ниже приведён контекст из проектной документации. "
        "Используй только его, если он релевантен. "
        "Если в контексте нет ответа — скажи, что данных недостаточно, и уточни, что нужно предоставить.\n\n"
        f"КОНТЕКСТ:\n{ctx}\n\n"
        f"ВОПРОС:\n{question}\n"
    )


def answer_question(
    *,
    question: str,
    context_chunks: List[str],
    system_prompt: str,
    model: str,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
    # NEW:
    # - file_paths: локальные пути, которые нужно загрузить в proxy и прикрепить к сообщению
    # - files: уже готовые file-объекты (как в chat.js/streamlit) или результаты upload_file()
    file_paths: Optional[Sequence[str]] = None,
    files: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """
    Отвечает на вопрос через Qwen-proxy.

    Поддерживает вложения:
      * file_paths=[...]  -> загрузит файлы через /files/upload и прикрепит как messages[].files
      * files=[...]       -> можно передать уже готовые file-объекты (или raw upload-resp; они будут нормализованы)

    Если вложения не нужны — просто не передавайте эти параметры.
    """
    client = QwenProxyClient(
        base_url=(base_url or os.getenv("QWEN_PROXY_BASE_URL", DEFAULT_BASE_URL)),
        timeout=float(timeout or os.getenv("QWEN_TIMEOUT", str(DEFAULT_TIMEOUT))),
    )

    file_objs: List[Dict[str, Any]] = []

    # 1) raw/given files
    if files:
        for f in files:
            # If it's already in expected shape (type=file or has url+file/meta), keep it.
            if isinstance(f, dict) and f.get("type") == "file" and "file" in f:
                file_objs.append(dict(f))
            else:
                # assume it's an upload response (raw) and normalize
                file_objs.append(client.build_file_obj(f))  # type: ignore[arg-type]

    # 2) upload local paths
    if file_paths:
        for path in file_paths:
            upload_resp = client.upload_file(str(path))
            file_objs.append(client.build_file_obj(upload_resp))

    user_msg: Dict[str, Any] = {"role": "user", "content": build_prompt(question, context_chunks)}
    if file_objs:
        user_msg["files"] = file_objs

    messages = [
        {"role": "system", "content": system_prompt},
        user_msg,
    ]

    try:
        resp, _lat = client.chat_completions(model=model, messages=messages, extra={"stream": False})
    except Exception as exc:
        if not _should_try_chat_endpoint(exc):
            raise
        resp, _lat = client.chat(model=model, messages=messages)

    # Proxy response formats may vary; handle common shapes
    if isinstance(resp, dict):
        choices = resp.get("choices")
        if choices and isinstance(choices, list):
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if content:
                return str(content)
        if resp.get("answer"):
            return str(resp["answer"])
        if resp.get("text"):
            return str(resp["text"])

    return str(resp)
