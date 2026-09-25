from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any


_SPACES_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


def normalize_alias(value: str | None) -> str:
    """Normalize marks/names for deterministic alias matching."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    text = text.replace("ё", "е")
    text = text.replace("–", "-").replace("—", "-").replace("_", "-")
    text = _SPACES_RE.sub(" ", text)
    text = _PUNCT_RE.sub("", text)
    return text


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    text = text.replace("ё", "е")
    text = text.replace("–", "-").replace("—", "-")
    text = _SPACES_RE.sub(" ", text)
    return text


def text_similarity(left: str | None, right: str | None) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def comparable_value(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, float):
        return f"{raw:.6g}"
    return normalize_text(str(raw))


def value_similarity(left: Any, right: Any) -> float:
    a = comparable_value(left)
    b = comparable_value(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def location_similarity(left: dict[str, Any] | None, right: dict[str, Any] | None) -> float:
    if not left or not right:
        return 0.0
    keys = set(left.keys()) | set(right.keys())
    if not keys:
        return 0.0
    matched = 0.0
    for key in keys:
        if key not in left or key not in right:
            continue
        if normalize_text(str(left[key])) == normalize_text(str(right[key])):
            matched += 1.0
    return matched / len(keys)

