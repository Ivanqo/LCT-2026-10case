"""Protocol export to DOCX and XML (TZ module 7: "экспорт протоколов в PDF, DOCX, XML").

One source for all three formats: the protocol's `payload.annex_2` (built by `protocol_annex2.annex2_payload`) plus the
protocol's own header fields. `build_export_model` turns that into a plain, JSON-serialisable model; `render_docx` and
`render_xml` only draw the model, so what a file says is what the JSON endpoint says. No new dependencies: DOCX is a zip
of WordprocessingML parts written with `zipfile`, XML is built with `xml.etree.ElementTree`.

The model keeps what the protocol is for: the sections of Annex 2, finding cards with their sources (file, SHA-256, page,
bbox), the values per stage, the status, the inspector's decisions with reasons, the output tier of each finding and the
free hypotheses (TZ 9.5) in a section of their own. A violation probability is never written: the pipeline has none.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from io import BytesIO
import re
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

from . import evidence_tier as et
from .protocol_annex2 import PROTOCOL_LABELS

XML_SCHEMA_VERSION = "case10-protocol-xml-v1"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XML_MEDIA_TYPE = "application/xml"

CARD_SECTIONS = (
    ("section_3_unverified_parameters", "3", "Непроверенные параметры"),
    ("section_4_critical_violations", "4", "Критические нарушения"),
    ("section_5_substantial_violations", "5", "Существенные нарушения"),
)
HYPOTHESIS_METHOD_TITLES = {
    "UNMATCHED_DIFF": "расхождение несопоставленных объектов ПД/РД",
    "SEMANTIC_DISSONANCE": "семантическое расхождение формулировок",
    "NORMATIVE_ANALYSIS": "анализ по нормативным требованиям",
    "LOGICAL_ANALYSIS": "логический анализ (правила Logical_Rules)",
    "CHANGE_SHEET": "лист изменений",
    "LAYER_COMPOSITION": "состав слоёв",
    "GRAPHIC_DIFF": "растровое сравнение листов",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


# ------------------------------------------------------------------------------------------------ model


def label(value: Any) -> Any:
    return PROTOCOL_LABELS.get(value, value) if isinstance(value, str) else value


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _clean(value: Any) -> str:
    """Text of a value for a document; None -> empty string (an empty field is shown as 'не указано' by the renderer)."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_clean(item) for item in value if item is not None)
    if isinstance(value, Mapping):
        return "; ".join(f"{key}={_clean(item)}" for key, item in value.items() if item is not None)
    return _CONTROL.sub("", str(value))


def _source(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "file": item.get("file") or item.get("file_id"),
        "file_id": item.get("file_id"),
        "sha256": item.get("file_sha256") or item.get("sha256"),
        "stage": item.get("dataset_stage") or item.get("stage"),
        "document_code": item.get("document_code"),
        "page": item.get("page") or item.get("pdf_page_number"),
        "bbox": item.get("bbox_normalized") or item.get("bbox_norm") or item.get("bbox"),
        "revision": item.get("revision"),
        "approval_status": item.get("approval_status"),
        "role": item.get("role"),
        "extracted_value": item.get("extracted_value"),
    }


def card_model(check: Mapping[str, Any]) -> dict[str, Any]:
    """One finding card from a `checks[]`-style row of the annex."""
    delta = check.get("delta") if isinstance(check.get("delta"), Mapping) else {}
    explanation = delta.get("explanation") if isinstance(delta.get("explanation"), Mapping) else {}
    tier = check.get("evidence_tier")
    result = delta.get("reason") or check.get("protocol_status")
    method = delta.get("discovery_method") or (delta.get("source") if delta.get("matrix_scope") == "FREE_SEARCH" else None)
    return {
        "evidence_group_id": check.get("evidence_group_id"),
        "finding_id": check.get("finding_id"),
        "parameter_code": check.get("parameter_code"),
        "parameter_name": check.get("parameter_name"),
        "location": check.get("location"),
        "finding_status": check.get("finding_status"),
        "finding_status_label": label(check.get("finding_status")),
        "protocol_status": check.get("protocol_status"),
        "violation_label": check.get("violation_label"),
        "inspector_status": check.get("inspector_status"),
        "inspector_status_label": label(check.get("inspector_status")),
        "criticality": check.get("criticality"),
        "tier": tier,
        "tier_title": et.TIER_TITLES.get(tier, tier),
        "tier_reasons": list(delta.get("tier_reason_text") or []),
        "method": method,
        "method_title": HYPOTHESIS_METHOD_TITLES.get(str(method), method) if method else None,
        "values": {"PD": check.get("pd_value"), "RD": check.get("rd_value"), "ID": check.get("id_value")},
        "result": label(result),
        "explanation": {
            "rule": explanation.get("rule"),
            "threshold": explanation.get("threshold"),
            "threshold_basis": explanation.get("threshold_basis"),
            "deviation_abs": explanation.get("deviation_abs"),
            "deviation_pct": explanation.get("deviation_pct"),
            "deviation_basis": explanation.get("deviation_basis"),
            "text_confidence": explanation.get("text_confidence"),
            "link_confidence": explanation.get("link_confidence"),
        },
        "sources": [_source(item) for item in check.get("evidence") or [] if isinstance(item, Mapping)],
        "decisions": [
            {"user_id": item.get("user_id"), "created_at": item.get("created_at"), "decision": item.get("decision"),
             "decision_label": label(item.get("decision")), "reason_code": item.get("reason_code"),
             "reason_label": label(item.get("reason_code")), "comment": item.get("comment")}
            for item in check.get("decisions") or [] if isinstance(item, Mapping)
        ],
    }


def build_export_model(protocol: Mapping[str, Any]) -> dict[str, Any]:
    """`protocol` is `protocol_to_dict(...)` (with payload). Returns the model both renderers draw."""
    payload = protocol.get("payload") or {}
    annex = payload.get("annex_2") or {}
    upload = annex.get("section_1_document_upload") or {}
    integrity = upload.get("integrity") or {}
    cards = {key: [card_model(row) for row in annex.get(key) or []] for key, _, _ in CARD_SECTIONS}
    hypotheses = [card_model(row) for row in annex.get("section_6_ai_suspicions") or []]
    pending = [card_model(row) for row in annex.get("pending_candidates") or []]
    model = {
        "schema_version": XML_SCHEMA_VERSION,
        "source_schema_version": annex.get("schema_version"),
        "protocol": {
            "id": protocol.get("id"), "process_id": protocol.get("process_id"), "version": protocol.get("version"),
            "status": protocol.get("status"), "created_at": protocol.get("created_at"),
            "finalized_at": protocol.get("finalized_at"), "matrix_version": protocol.get("matrix_version"),
            "dataset_version": protocol.get("dataset_version"), "model_version": protocol.get("model_version"),
            "input_manifest_hash": protocol.get("input_manifest_hash"), "object_id": protocol.get("object_id"),
            "signature_status": (annex.get("header") or {}).get("signature_status") or "UNSIGNED",
        },
        "header": {key: (annex.get("header") or {}).get(key) for key in
                   ("object_name", "address", "supervisory_case_number", "developer", "contractor")},
        "section_1": {
            "uploads": [{"stage": row.get("stage"), "status": row.get("status"), "status_label": label(row.get("status")),
                         "loaded_files": row.get("loaded_files"), "expected_files": row.get("expected_files"),
                         "excluded_files": row.get("excluded_files"), "comment": row.get("comment")}
                        for row in upload.get("rows") or []],
            "mixed_stage_files": upload.get("mixed_stage_files"),
            "unknown_stage_files": upload.get("unknown_stage_files"),
            "excluded": [{"file_id": item.get("file_id"), "stage": item.get("stage"), "reason": item.get("reason"),
                          "reason_label": label(item.get("reason")), "duplicate_of": item.get("duplicate_of")}
                         for item in integrity.get("excluded") or []],
            "revision_conflicts": [{"stage": item.get("stage"), "scope": item.get("scope"), "type": item.get("type"),
                                    "type_label": label(item.get("type")), "file_ids": list(item.get("file_ids") or [])}
                                   for item in upload.get("revision_conflicts") or []],
            "live_tagger_summary": (payload.get("live_tagger_coverage") or {}).get("summary"),
        },
        "section_2": [{"name": str(row.get("name") or "").strip(), "count": row.get("count"), "percent_of_matrix": row.get("percent_of_matrix")}
                      for row in annex.get("section_2_statistics") or []],
        "section_3": cards["section_3_unverified_parameters"],
        "section_4": cards["section_4_critical_violations"],
        "section_5": cards["section_5_substantial_violations"],
        "section_6_hypotheses": hypotheses,
        "section_7": {"status": (annex.get("section_7_resolution") or {}).get("status"),
                      "items": [{"parameter_code": item.get("parameter_code"), "evidence_group_id": item.get("evidence_group_id"),
                                 "inspector_comment": item.get("inspector_comment"), "recommendation": item.get("recommendation")}
                                for item in (annex.get("section_7_resolution") or {}).get("items") or []]},
        "pending_candidates": pending,
        "evidence_tiers": annex.get("evidence_tiers") or {},
    }
    return _plain(model)


# ------------------------------------------------------------------------------------------------ XML

_ITEM_NAMES = {
    "uploads": "upload", "excluded": "file", "revision_conflicts": "conflict", "file_ids": "file_id", "items": "item",
    "sources": "source", "decisions": "decision", "tier_reasons": "reason", "section_2": "statistic",
    "section_3": "finding", "section_4": "finding", "section_5": "finding", "section_6_hypotheses": "hypothesis",
    "pending_candidates": "finding", "bbox": "value",
}


def _append(parent: ET.Element, tag: str, value: Any) -> None:
    """Dict -> children, list -> repeated <item-name>, scalar -> text; empty values stay as an empty element with
    `nil="true"` so the structure is the same for every protocol."""
    node = ET.SubElement(parent, tag)
    if isinstance(value, Mapping):
        for key, item in value.items():
            _append(node, str(key), item)
    elif isinstance(value, (list, tuple)):
        node.set("count", str(len(value)))
        child_tag = _ITEM_NAMES.get(tag, "item")
        for item in value:
            _append(node, child_tag, item)
    elif value is None:
        node.set("nil", "true")
    elif isinstance(value, bool):
        node.text = "true" if value else "false"
    else:
        node.text = _clean(value)


def render_xml(protocol: Mapping[str, Any]) -> bytes:
    model = build_export_model(protocol)
    root = ET.Element("protocol", {"schema_version": XML_SCHEMA_VERSION, "id": str(model["protocol"].get("id")),
                                   "version": str(model["protocol"].get("version")), "lang": "ru"})
    for key, value in model.items():
        if key == "schema_version":
            continue
        _append(root, key, value)
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


# ------------------------------------------------------------------------------------------------ DOCX

_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    "</Relationships>"
)
_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)


def _style(style_id: str, name: str, *, size: int, bold: bool = False, based_on: str | None = "Normal", before: int = 0, after: int = 80) -> str:
    return (f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>'
            + (f'<w:basedOn w:val="{based_on}"/>' if based_on else "")
            + f'<w:qFormat/><w:pPr><w:spacing w:before="{before}" w:after="{after}"/></w:pPr>'
            f'<w:rPr>{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr></w:style>')


def _styles_xml() -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {_NS}>'
            '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman" w:eastAsia="Times New Roman"/>'
            '<w:sz w:val="20"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault></w:docDefaults>'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>'
            '<w:pPr><w:spacing w:after="80"/></w:pPr></w:style>'
            + _style("Title", "Title", size=32, bold=True, after=160)
            + _style("Heading1", "heading 1", size=26, bold=True, before=240, after=120)
            + _style("Heading2", "heading 2", size=22, bold=True, before=200, after=80)
            + _style("Heading3", "heading 3", size=20, bold=True, before=160, after=60)
            + _style("Small", "Small", size=16)
            + '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders>'
            + "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="999999"/>' for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
            + '</w:tblBorders><w:tblCellMar><w:left w:w="70" w:type="dxa"/><w:right w:w="70" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
            "</w:styles>")


def _run(text: str, *, bold: bool = False, size: int | None = None) -> str:
    props = ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/>' if size else "")
    pieces = _clean(text).split("\n")
    body = "<w:br/>".join(f'<w:t xml:space="preserve">{escape(piece)}</w:t>' for piece in pieces)
    return f"<w:r>{'<w:rPr>' + props + '</w:rPr>' if props else ''}{body}</w:r>"


def _para(text: str, style: str | None = None, *, bold: bool = False) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{props}{_run(text, bold=bold)}</w:p>"


def _table(headers: list[str], rows: list[list[Any]], widths: list[int] | None = None) -> str:
    if not rows:
        return _para("Нет записей.")
    count = len(headers)
    widths = widths or [9000 // count] * count
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for width in widths)

    def cell(text: Any, width: int, *, bold: bool = False, shade: bool = False) -> str:
        shading = '<w:shd w:val="clear" w:color="auto" w:fill="E7E6E6"/>' if shade else ""
        return (f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shading}</w:tcPr>'
                f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>{_run(_clean(text) or "—", bold=bold, size=18)}</w:p></w:tc>')

    head = '<w:tr><w:trPr><w:tblHeader/></w:trPr>' + "".join(cell(h, w, bold=True, shade=True) for h, w in zip(headers, widths)) + "</w:tr>"
    body = "".join("<w:tr>" + "".join(cell(value, w) for value, w in zip(row, widths)) + "</w:tr>" for row in rows)
    return (f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr>'
            f"<w:tblGrid>{grid}</w:tblGrid>{head}{body}</w:tbl>" + _para(""))


def _card_blocks(card: Mapping[str, Any]) -> list[str]:
    out = [_para(f"{_clean(card.get('parameter_code'))} {_clean(card.get('parameter_name'))}".strip(), "Heading3")]
    line = (f"Место: {_clean(card.get('location')) or 'не указано'}. Статус: {_clean(card.get('finding_status_label')) or 'не указан'}; "
            f"решение инспектора: {_clean(card.get('inspector_status_label')) or 'не указано'}.")
    if card.get("tier_title"):
        line += f" Уровень выдачи: {_clean(card['tier_title'])}."
    if card.get("method_title"):
        line += f" Метод поиска: {_clean(card['method_title'])}."
    out.append(_para(line))
    if card.get("evidence_group_id"):
        out.append(_para(f"Идентификаторы: находка {_clean(card.get('finding_id'))}, группа доказательств {_clean(card['evidence_group_id'])}.", "Small"))
    if card.get("tier_reasons"):
        out.append(_para("Оговорка: " + "; ".join(_clean(item) for item in card["tier_reasons"])))
    explanation = card.get("explanation") or {}
    if explanation.get("rule") or explanation.get("threshold_basis"):
        out.append(_para(f"Основание: {_clean(explanation.get('rule'))}. Порог: {_clean(explanation.get('threshold')) or 'не задан'}. {_clean(explanation.get('threshold_basis'))}"))
    if explanation.get("deviation_abs") is not None or explanation.get("deviation_pct") is not None:
        out.append(_para(f"Отклонение: {_clean(explanation.get('deviation_abs'))} ({_clean(explanation.get('deviation_pct'))} %), {_clean(explanation.get('deviation_basis'))}."))
    text_conf, link_conf = explanation.get("text_confidence"), explanation.get("link_confidence")
    out.append(_para("Уверенность распознавания текста: " + (_clean(text_conf) if text_conf is not None else "не вычислялась")
                     + "; уверенность привязки: " + (_clean(link_conf) if link_conf is not None else "не вычислялась")
                     + ". Вероятность нарушения не заявляется (способа расчёта нет)."))
    values = card.get("values") or {}
    out.append(_table(["ПД", "РД", "ИД", "Результат"], [[values.get("PD"), values.get("RD"), values.get("ID"), card.get("result")]]))
    sources = card.get("sources") or []
    if sources:
        out.append(_para("Источники", bold=True))
        out.append(_table(["Файл", "SHA-256", "Стадия", "Лист / шифр", "Стр.", "bbox", "Редакция", "Значение"],
                          [[s.get("file"), s.get("sha256"), s.get("stage"), s.get("document_code"), s.get("page"),
                            s.get("bbox"), " ".join(x for x in (_clean(s.get("revision")), _clean(label(s.get("approval_status")))) if x),
                            s.get("extracted_value")] for s in sources],
                          [1500, 1900, 600, 1100, 450, 1250, 1000, 1200]))
    for decision in card.get("decisions") or []:
        out.append(_para(f"Решение инспектора {_clean(decision.get('user_id'))}, {_clean(decision.get('created_at'))}: "
                         f"{_clean(decision.get('decision_label'))}. Причина: {_clean(decision.get('reason_label')) or 'не указана'}. {_clean(decision.get('comment'))}"))
    return out


def _section_cards(cards: list[Mapping[str, Any]]) -> list[str]:
    if not cards:
        return [_para("Нет записей.")]
    out: list[str] = []
    for card in cards:
        out += _card_blocks(card)
    return out


def render_docx(protocol: Mapping[str, Any]) -> bytes:
    model = build_export_model(protocol)
    meta, header = model["protocol"], model["header"]
    s1 = model["section_1"]
    body = [
        _para(f"ПРОТОКОЛ АВТОМАТИЗИРОВАННОЙ СВЕРКИ № {_clean(meta.get('id'))}", "Title"),
        _para(f"Объект: {_clean(header.get('object_name') or meta.get('object_id')) or 'не указан'}\n"
              f"Адрес: {_clean(header.get('address')) or 'не указан'}\n"
              f"Номер надзорного дела: {_clean(header.get('supervisory_case_number')) or 'не указан'}\n"
              f"Застройщик: {_clean(header.get('developer')) or 'не указан'}\n"
              f"Подрядчик: {_clean(header.get('contractor')) or 'не указан'}\n"
              f"Версия: {_clean(meta.get('version'))}. Статус: {_clean(meta.get('status'))}. Создан: {_clean(meta.get('created_at'))}\n"
              f"Матрица: {_clean(meta.get('matrix_version'))}; набор данных: {_clean(meta.get('dataset_version'))}; "
              f"модель: {_clean(meta.get('model_version'))}\n"
              f"Хэш входного комплекта: {_clean(meta.get('input_manifest_hash')) or 'не указан'}"),
        _para("Электронная подпись не наложена. Финализация фиксирует неизменяемую версию в системе."),
        _para("РАЗДЕЛ 1. СТАТУС ЗАГРУЗКИ ДОКУМЕНТОВ", "Heading1"),
        _table(["Тип", "Статус", "Загружено", "Ожидается", "Исключено"],
               [[r.get("stage"), r.get("status_label"), r.get("loaded_files"), r.get("expected_files"), r.get("excluded_files")] for r in s1["uploads"]]),
        _para(f"Смешанная стадия РД/ИД: {_clean(s1.get('mixed_stage_files')) or 0}. Стадия не определена: {_clean(s1.get('unknown_stage_files')) or 0}."),
        _para("Исключённые из сравнения файлы", "Heading3"),
        _table(["Файл", "Стадия", "Причина", "Дубль файла"], [[i.get("file_id"), i.get("stage"), i.get("reason_label"), i.get("duplicate_of")] for i in s1["excluded"]]),
        _para("Конфликты редакций", "Heading3"),
        _table(["Стадия", "Раздел", "Тип", "Файлы"], [[c.get("stage"), c.get("scope"), c.get("type_label"), ", ".join(c.get("file_ids") or [])] for c in s1["revision_conflicts"]]),
    ]
    if s1.get("live_tagger_summary"):
        body.append(_para(s1["live_tagger_summary"]))
    body += [_para("РАЗДЕЛ 2. СВОДНАЯ СТАТИСТИКА", "Heading1"),
             _table(["Показатель", "Количество", "% от Матрицы"], [[r["name"], r["count"], r["percent_of_matrix"]] for r in model["section_2"]], [5400, 1800, 1800])]
    for key, number, title in CARD_SECTIONS:
        body += [_para(f"РАЗДЕЛ {number}. {title.upper()}", "Heading1"), *_section_cards(model[f"section_{number}"])]
    body += [_para("РАЗДЕЛ 6. СВОБОДНЫЕ ГИПОТЕЗЫ И ПОДОЗРЕНИЯ ИИ (вне Матрицы)", "Heading1"),
             _para("Гипотезы выдаёт система для проверки инспектором и не считает нарушениями; решение принимает инспектор."),
             *_section_cards(model["section_6_hypotheses"]),
             _para("РАЗДЕЛ 7. РЕЗОЛЮТИВНАЯ ЧАСТЬ", "Heading1"),
             _table(["Параметр", "Комментарий инспектора", "Рекомендация"],
                    [[r.get("parameter_code"), r.get("inspector_comment"), r.get("recommendation")] for r in model["section_7"]["items"]], [2000, 4500, 2500]),
             _para("КАНДИДАТЫ, ОЖИДАЮЩИЕ ПРОВЕРКИ", "Heading1"),
             *_section_cards(model["pending_candidates"])]
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_NS}><w:body>' + "".join(body)
                + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720" w:header="400" w:footer="400" w:gutter="0"/></w:sectPr>'
                "</w:body></w:document>")
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape('Протокол сверки № ' + _clean(meta.get('id')))}</dc:title><dc:creator>CASE10 AI-inspector</dc:creator></cp:coreProperties>")
    buffer = BytesIO()
    # a fixed timestamp in every zip entry: the same protocol gives the same bytes
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, data in (("[Content_Types].xml", _CONTENT_TYPES), ("_rels/.rels", _ROOT_RELS), ("word/document.xml", document),
                           ("word/_rels/document.xml.rels", _DOC_RELS), ("word/styles.xml", _styles_xml()), ("docProps/core.xml", core)):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, data.encode("utf-8"))
    return buffer.getvalue()
