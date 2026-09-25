from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from ..db.models import DocumentVersion, Param, SourceFragment


@dataclass(slots=True)
class ExtractionResult:
    value: str
    normalized_value: str
    confidence: float
    file: str
    page: int | None
    bbox: list[float] | None
    extractor: str
    document_version_id: int
    source_fragment_id: int | None
    context: str | None = None


class ParameterExtractor:
    name = "base"

    def extract(
        self,
        *,
        param: Param,
        document: DocumentVersion,
        fragments: Iterable[SourceFragment],
    ) -> list[ExtractionResult]:
        raise NotImplementedError


class RegexExtractor(ParameterExtractor):
    name = "regex"

    def extract(
        self,
        *,
        param: Param,
        document: DocumentVersion,
        fragments: Iterable[SourceFragment],
    ) -> list[ExtractionResult]:
        pattern = str(param.regex_pattern or "").strip()
        if not pattern:
            return []

        out: list[ExtractionResult] = []
        try:
            rx = re.compile(pattern, flags=re.IGNORECASE | re.MULTILINE)
        except re.error:
            return []

        for fragment in fragments:
            text = str(fragment.text or "")
            if not text:
                continue
            match = rx.search(text)
            if not match:
                continue
            value = _match_value(match)
            if not value:
                continue
            out.append(
                ExtractionResult(
                    value=value,
                    normalized_value=_normalize_value(value),
                    confidence=float(fragment.confidence or 0.7),
                    file=str(document.filename or ""),
                    page=int(fragment.page) if fragment.page is not None else None,
                    bbox=_normalize_bbox(fragment.bbox, fragment.metadata_json),
                    extractor=self.name,
                    document_version_id=int(document.id),
                    source_fragment_id=int(fragment.id) if fragment.id is not None else None,
                    context=_clip(text, 900),
                )
            )
        return out


class SemanticExtractor(ParameterExtractor):
    name = "semantic"

    def extract(self, *, param: Param, document: DocumentVersion, fragments: Iterable[SourceFragment]) -> list[ExtractionResult]:
        return []


class TableExtractor(ParameterExtractor):
    name = "table"

    def extract(self, *, param: Param, document: DocumentVersion, fragments: Iterable[SourceFragment]) -> list[ExtractionResult]:
        return []


class OCRExtractor(ParameterExtractor):
    name = "ocr"

    def extract(self, *, param: Param, document: DocumentVersion, fragments: Iterable[SourceFragment]) -> list[ExtractionResult]:
        return []


class CVExtractor(ParameterExtractor):
    name = "cv"

    def extract(self, *, param: Param, document: DocumentVersion, fragments: Iterable[SourceFragment]) -> list[ExtractionResult]:
        return []


def default_extractors() -> list[ParameterExtractor]:
    return [RegexExtractor(), TableExtractor(), SemanticExtractor(), OCRExtractor(), CVExtractor()]


def _match_value(match: re.Match[str]) -> str:
    groups = match.groupdict()
    if groups.get("value") is not None:
        value = str(groups["value"])
    elif any(v is not None for v in groups.values()):
        value = str(next(v for v in groups.values() if v is not None))
    elif match.lastindex:
        value = str(match.group(1))
    else:
        value = str(match.group(0))
    unit = str(groups.get("unit") or "").strip()
    value = value.strip()
    if unit and not value.lower().endswith(unit.lower()):
        value = f"{value} {unit}"
    return value


def _normalize_value(value: object) -> str:
    text = str(value or "").strip().replace(",", ".")
    return re.sub(r"\s+", " ", text)


def _normalize_bbox(raw_bbox: object, metadata: object = None) -> list[float] | None:
    if not isinstance(raw_bbox, list) or len(raw_bbox) < 4:
        return None
    values: list[float] = []
    try:
        values = [float(raw_bbox[i]) for i in range(4)]
    except Exception:
        return None
    if all(0.0 <= value <= 1.0 for value in values):
        return [round(max(0.0, min(1.0, value)), 6) for value in values]

    meta = metadata if isinstance(metadata, dict) else {}
    width = _positive_float(meta.get("width") or meta.get("page_width"))
    height = _positive_float(meta.get("height") or meta.get("page_height"))
    if not width or not height:
        max_x = max(values[0], values[2], 1.0)
        max_y = max(values[1], values[3], 1.0)
        width = max(max_x, 1000.0)
        height = max(max_y, 1000.0)
    normalized = [values[0] / width, values[1] / height, values[2] / width, values[3] / height]
    return [round(max(0.0, min(1.0, value)), 6) for value in normalized]


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "..."
