from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from shared.llm import QwenProxyClient, DEFAULT_BASE_URL, DEFAULT_TIMEOUT

_JSON_BLOCK_RX = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_JSON_INLINE_RX = re.compile(r"(\{.*\})", re.DOTALL)


def _extract_text(resp: Any) -> str:
    if isinstance(resp, dict):
        choices = resp.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        value = item.get("text") or item.get("content")
                        if isinstance(value, str) and value.strip():
                            parts.append(value.strip())
                    elif isinstance(item, str) and item.strip():
                        parts.append(item.strip())
                if parts:
                    return "\n".join(parts).strip()
        for key in ("answer", "text", "content", "response"):
            val = resp.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return str(resp).strip()


def _parse_structured_payload(text: str) -> Dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    m = _JSON_BLOCK_RX.search(text)
    if m:
        candidates.insert(0, m.group(1).strip())
    m2 = _JSON_INLINE_RX.search(text)
    if m2:
        candidates.insert(0, m2.group(1).strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


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


def _normalize_chunk_ids(ids: Iterable[Any], allowed_ids: set[int]) -> List[int]:
    out: List[int] = []
    seen: set[int] = set()
    for raw in ids:
        try:
            value = int(raw)
        except Exception:
            continue
        if value not in allowed_ids or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _truncate(text: str, limit: int = 2200) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _sanitize_answer(answer: str) -> str:
    answer = str(answer or "").strip()
    patterns = [
        r"CHUNK_ID\s*=\s*\d+",
        r"chunk_id\s*=\s*\d+",
        r"chunk\s*\d+",
        r"score\s*[=:]\s*[-+]?\d+(?:[.,]\d+)?",
        r"\b(region|asset|embedding|retriever|context)\b",
    ]
    for pattern in patterns:
        answer = re.sub(pattern, "", answer, flags=re.IGNORECASE)
    answer = re.sub(r"\n{3,}", "\n\n", answer)
    answer = re.sub(r"[ \t]{2,}", " ", answer)
    return answer.strip(" \n-–—:;,.")


def _format_reference_lines(chunks: Sequence[Dict[str, Any]], chunk_ids: Sequence[int]) -> str:
    lines: list[str] = []
    seen: set[tuple[str, int]] = set()
    for chunk in chunks:
        cid = chunk.get("chunk_id")
        if cid not in chunk_ids:
            continue
        key = (str(chunk.get("filename") or "Документ"), int(chunk.get("page_number") or 0))
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {key[0]}, лист {key[1]}")
    return "\n".join(lines[:6])


def _fallback_answer(question: str, chunks: Sequence[Dict[str, Any]]) -> str:
    first = chunks[0] if chunks else {}
    excerpt = _truncate(str(first.get("text") or ""), 400)
    lines: List[str] = [
        "Нашёл наиболее близкие фрагменты в документации.",
    ]
    if excerpt:
        lines.append(f"Ключевой найденный фрагмент: {excerpt}")
    else:
        lines.append("По текущему контексту не удалось собрать точный ответ.")
    refs = _format_reference_lines(chunks, [int(c["chunk_id"]) for c in chunks[:3] if c.get("chunk_id") is not None])
    if refs:
        lines.append("\nГде смотреть в документации:\n" + refs)
    lines.append(f"\nВопрос: {question}")
    return "\n".join(lines).strip()


def ask_structured_answer(
    *,
    question: str,
    chunks: Sequence[Dict[str, Any]],
    assets: Sequence[Dict[str, Any]],
    system_prompt: str,
    model: str,
) -> Tuple[str, List[int], List[int], str]:
    del assets  # new retriever is text-only for now
    allowed_chunk_ids = {int(c["chunk_id"]) for c in chunks if c.get("chunk_id") is not None}

    text_parts: List[str] = []
    for chunk in chunks:
        chunk_id = chunk.get("chunk_id")
        filename = chunk.get("filename") or "document"
        page_number = chunk.get("page_number") or 0
        text = _truncate(str(chunk.get("text") or ""), 2600)
        if not text:
            continue
        text_parts.append(
            f"[CHUNK_ID={chunk_id}; FILE={filename}; PAGE={page_number}]\n{text}"
        )

    prompt = (
        "Тебе переданы текстовые блоки из проектной документации. Каждый блок привязан к конкретному листу документа.\n"
        "Нужно ответить на вопрос инженеру понятным профессиональным русским языком.\n\n"
        "Верни СТРОГО один JSON-объект без markdown-обёрток и без текста вне JSON.\n"
        "Формат ответа:\n"
        '{"answer": "понятный инженерный ответ на русском", "chunk_ids": [1,2,3], "asset_ids": []}\n\n'
        "Правила для поля answer:\n"
        "- сначала дай прямой ответ на вопрос;\n"
        "- затем кратко поясни, что именно найдено в документации;\n"
        "- если данных недостаточно, честно скажи об этом;\n"
        "- не показывай пользователю внутренние идентификаторы, служебные слова и JSON;\n"
        "- не придумывай данные, которых нет в найденных фрагментах;\n"
        "- выбирай только те chunk_ids, которые действительно подтверждают ответ.\n\n"
        f"ВОПРОС:\n{question}\n\n"
        "TEXT_BLOCKS:\n" + ("\n\n---\n\n".join(text_parts) if text_parts else "нет")
    )

    client = QwenProxyClient(
        base_url=os.getenv("QWEN_PROXY_BASE_URL", DEFAULT_BASE_URL),
        timeout=float(os.getenv("QWEN_TIMEOUT", str(DEFAULT_TIMEOUT))),
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    try:
        response, _ = client.chat_completions(
            model=model,
            messages=messages,
            extra={"temperature": 0.1, "stream": False},
        )
    except Exception as exc:
        if not _should_try_chat_endpoint(exc):
            raise
        response, _ = client.chat(
            model=model,
            messages=messages,
            extra={"temperature": 0.1, "stream": False},
        )
    raw_text = _extract_text(response)
    parsed = _parse_structured_payload(raw_text)

    if parsed:
        answer = _sanitize_answer(str(parsed.get("answer") or "").strip())
        chunk_ids = _normalize_chunk_ids(parsed.get("chunk_ids") or [], allowed_chunk_ids)
        if answer and len(re.sub(r"\s+", " ", answer)) >= 24:
            refs = _format_reference_lines(chunks, chunk_ids)
            if refs and "Где смотреть" not in answer and "Где это" not in answer:
                answer = f"{answer}\n\nГде смотреть в документации:\n{refs}"
            return answer, chunk_ids, [], raw_text

    fallback_chunk_ids = [int(c["chunk_id"]) for c in chunks[: min(4, len(chunks))] if c.get("chunk_id") is not None]
    fallback_answer = _sanitize_answer(raw_text.strip()) if len(re.sub(r"\s+", " ", raw_text or "")) >= 24 else _fallback_answer(question, chunks)
    refs = _format_reference_lines(chunks, fallback_chunk_ids)
    if refs and "Где смотреть" not in fallback_answer and "Где это" not in fallback_answer:
        fallback_answer = f"{fallback_answer}\n\nГде смотреть в документации:\n{refs}"
    return (
        fallback_answer,
        _normalize_chunk_ids(fallback_chunk_ids, allowed_chunk_ids),
        [],
        raw_text,
    )
