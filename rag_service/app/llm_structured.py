from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple


def _truncate(text: str, limit: int = 2200) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _format_reference_lines(chunks: Sequence[Dict[str, Any]]) -> str:
    lines: list[str] = []
    seen: set[tuple[str, int]] = set()
    for chunk in chunks:
        key = (str(chunk.get("filename") or "Документ"), int(chunk.get("page_number") or 0))
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {key[0]}, лист {key[1]}")
    return "\n".join(lines[:6])


def ask_structured_answer(
    *,
    question: str,
    chunks: Sequence[Dict[str, Any]],
    assets: Sequence[Dict[str, Any]],
) -> Tuple[str, List[int], List[int], str]:
    """Return retrieved evidence without calling an external text-generation service."""
    del assets  # The current retriever is text-only.
    selected_ids = [int(chunk["chunk_id"]) for chunk in chunks[:4] if chunk.get("chunk_id") is not None]
    first = chunks[0] if chunks else {}
    excerpt = _truncate(str(first.get("text") or ""), 400)
    lines = ["Автоматическая генерация ответа отключена; показываю найденный контекст."]
    if excerpt:
        lines.append(f"Ключевой фрагмент: {excerpt}")
    else:
        lines.append("В найденных фрагментах нет текстового содержимого.")
    references = _format_reference_lines(chunks)
    if references:
        lines.append("\nГде смотреть в документации:\n" + references)
    lines.append(f"\nВопрос: {question}")
    return "\n".join(lines).strip(), selected_ids, [], ""
