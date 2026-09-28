"""Content-first classification of ventilation drawing sheets.

The file registry is authoritative when it names a discipline. Organizer
metadata and file names are discovery hints only; a page is accepted as an
HVAC sheet from its own title/stamp or from a room-and-system drawing pattern.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any


_VENTILATION_TITLE_PATTERNS = (
    re.compile(r"\bплан\b.{0,100}\bвентиляц", re.IGNORECASE),
    re.compile(r"\bпринципиаль\w*.{0,140}\b(?:общеобмен\w*|вентиляц\w*|приточ\w*)", re.IGNORECASE),
    re.compile(r"\b(?:общеобмен\w*|вентиляц\w*|приточ\w*).{0,140}\bпринципиаль\w*", re.IGNORECASE),
)
_HEATING_TITLE_PATTERNS = (
    re.compile(r"\bплан\b.{0,100}\b(?:отоплен|теплоснабж|тепл\w*\s+пол)", re.IGNORECASE),
    re.compile(r"\bпринципиаль\w*.{0,120}\b(?:отоплен|теплоснабж)", re.IGNORECASE),
)
_VENTILATION_STAMP = re.compile(
    r"(?<![\wА-Яа-я])(?:\d+(?:[.-]\d+)*[-_ ]*)?(?:ПД|РД|ИД)?[-_ ]*ОВ\s*\d*(?:\.\d+)?(?![\wА-Яа-я])",
    re.IGNORECASE,
)
_ROOM_NUMBER = re.compile(r"(?<![\wА-Яа-я])(?:\d{1,4}(?:\.\d{1,3})?|[А-ЯA-Z]\d{1,3}(?:\.\d{1,3})?)(?![\wА-Яа-я])")
_SYSTEM_LABEL = re.compile(
    r"(?<![\wА-Яа-я])(?:[PРП]\s*\d{1,2}(?:[.,]\s*\d{1,2})?|[ВVB]\s*(?:Е|E)|[ВVB]\s*\d{1,2}(?:[.,]\s*\d{1,2})?)(?![\wА-Яа-я])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DrawingClassification:
    discipline: str
    confidence: float
    signals: tuple[str, ...]


def _manifest(document: Any) -> dict[str, Any]:
    metadata = getattr(document, "dataset_metadata", None) or {}
    row = metadata.get("document_manifest") or {}
    return row if isinstance(row, dict) else {}


def registry_record(document: Any) -> dict[str, Any]:
    value = _manifest(document).get("registry")
    return value if isinstance(value, dict) else {}


def registry_discipline(document: Any) -> str | None:
    """Return an explicitly registered discipline, if the registry provides one."""
    value = registry_record(document).get("discipline")
    return str(value).strip() if value not in (None, "") else None


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def is_ventilation_discipline(value: Any) -> bool:
    folded = _fold(value)
    compact = re.sub(r"[^a-zа-я0-9]+", "", folded)
    return (
        compact in {"ов", "ов1", "ов2", "hvac", "heatingventilation"}
        or "отоплениеивентиляц" in compact
        or "вентиляция" == folded
        or "ventilation" in folded
    )


def registry_excludes_ventilation(document: Any) -> bool:
    discipline = registry_discipline(document)
    return discipline is not None and not is_ventilation_discipline(discipline)


def document_discovery_hints(document: Any) -> tuple[str, ...]:
    """Positive hints used to prioritize page scans; none replaces page content."""
    if registry_excludes_ventilation(document):
        return ()
    hints: list[str] = []
    discipline = registry_discipline(document)
    if discipline and is_ventilation_discipline(discipline):
        hints.append("registry_discipline")
    metadata = getattr(document, "dataset_metadata", None) or {}
    manifest = _manifest(document)
    organizer_sections = (
        getattr(document, "dataset_section", None), manifest.get("section"),
        (metadata.get("files_index") or {}).get("section"),
    )
    if any(is_ventilation_discipline(section) for section in organizer_sections if section):
        hints.append("organizer_section")
    name_text = " ".join(
        str(value or "") for value in (
            manifest.get("relative_path"), manifest.get("source_relative_path"),
            manifest.get("package_path"), getattr(document, "filename", None),
            getattr(document, "document_code", None),
        )
    )
    if _VENTILATION_STAMP.search(_fold(name_text)):
        hints.append("file_code")
    if any(token in _fold(name_text) for token in ("вентиляц", "воздуховод", "венткамера", "ventilat", "hvac")):
        hints.append("file_title")
    return tuple(dict.fromkeys(hints))


def classify_ventilation_sheet(snapshot: dict[str, Any], document: Any | None = None) -> DrawingClassification | None:
    """Classify one page from page content, with registry discipline as a guard."""
    return classify_ventilation_text(snapshot.get("text") or "", document)


def classify_ventilation_text(text_value: Any, document: Any | None = None) -> DrawingClassification | None:
    """Classify extracted page text before loading word geometry for a candidate."""
    if document is not None and registry_excludes_ventilation(document):
        return None
    text = _fold(text_value)
    signals: list[str] = []
    if any(pattern.search(text) for pattern in _VENTILATION_TITLE_PATTERNS):
        signals.append("ventilation_title")
    if any(pattern.search(text) for pattern in _HEATING_TITLE_PATTERNS):
        signals.append("heating_title")
    if _VENTILATION_STAMP.search(text):
        signals.append("discipline_stamp")
    if re.search(r"\b(?:венткамера|воздуховод|местн\w*\s+отсос)\b", text):
        signals.append("ventilation_terms")
    if re.search(r"\bтепл\w*\s+пол\w*\b", text):
        signals.append("warm_floor_terms")
    rooms = _ROOM_NUMBER.findall(text)
    systems = _SYSTEM_LABEL.findall(text)
    if len(rooms) >= 1 and len(systems) >= 2:
        signals.append("room_system_layout")
    if signals:
        confidence = 0.98 if any(signal in signals for signal in ("ventilation_title", "heating_title", "discipline_stamp")) else 0.84
        return DrawingClassification("OV", confidence, tuple(signals))
    return None


def is_ventilation_drawing_page(text_value: Any, document: Any | None = None) -> bool:
    """Whether a classified HVAC page contains a plan/scheme worth extracting.

    A discipline stamp establishes that the page belongs to ОВ, but appears on
    schedules and specification pages too. Detailed geometry is limited to
    titled plans/schemes or pages with room and system labels.
    """
    classification = classify_ventilation_text(text_value, document)
    if classification is None:
        return False
    return any(signal in classification.signals for signal in (
        "ventilation_title", "heating_title", "room_system_layout",
    ))


def is_candidate_pdf(document: Any) -> bool:
    manifest = _manifest(document)
    path = str(manifest.get("relative_path") or manifest.get("source_relative_path") or "")
    if not path:
        path = str(getattr(document, "filename", "") or "")
    return path.casefold().endswith(".pdf")


def candidate_document_stage(document: Any) -> str:
    raw = str(getattr(document, "dataset_stage", "") or "").upper()
    if raw in {"PD", "RD", "ID"}:
        return raw
    return {"project": "PD", "working": "RD", "as_built": "ID"}.get(
        str(getattr(document, "doc_stage", "") or ""), "UNKNOWN"
    )


def drawing_sheet_bounds(snapshot: dict[str, Any]) -> bool:
    """Cheap evidence that a classified page contains a room plan or system schematic."""
    text = _fold(snapshot.get("text") or "")
    return bool(
        _VENTILATION_STAMP.search(text)
        or any(pattern.search(text) for pattern in (*_VENTILATION_TITLE_PATTERNS, *_HEATING_TITLE_PATTERNS))
    )
