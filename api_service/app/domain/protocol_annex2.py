from __future__ import annotations

from collections import Counter
from html import escape
from io import BytesIO

from evaluation.exporter import evidence_group_to_submission_check


def annex2_payload(findings, docs, *, object_info, matrix_size, document_analysis=None):
    confirmed = [row for row in findings if row["finding_status"] == "CONFIRMED_VIOLATION"]
    critical = [row for row in confirmed if row["review_priority"] == "HIGH"]
    substantial = [row for row in confirmed if row["review_priority"] != "HIGH"]
    pending = [row for row in findings if row["finding_status"] == "CANDIDATE"]
    missing = [row for row in findings if row["finding_status"] in {"MISSING_EVIDENCE", "NOT_COMPARABLE", "CLARIFICATION_REQUIRED"}]
    suspicions = [row for row in findings if row["finding_status"] == "SUSPICION" or (row.get("delta") or {}).get("matrix_scope") == "FREE_SEARCH"]
    stages = Counter(doc.dataset_stage or {"project": "PD", "working": "RD", "as_built": "ID"}.get(doc.doc_stage, "UNKNOWN") for doc in docs)
    uploads = [{"stage": stage, "status": f"{stage}_UPLOADED" if stages[stage] else f"{stage}_MISSING",
                "loaded_files": stages[stage], "expected_files": None,
                "comment": "Ожидаемое количество файлов не задано"} for stage in ("PD", "RD", "ID")]
    checked = {row["parameter"]["code"] for row in findings if row["finding_status"] in {"NEGATIVE_VERIFIED", "CANDIDATE", "CONFIRMED_VIOLATION"}}
    # Комплектность / целостность (ТЗ 9.2, 10 баллов): files that were set aside (byte duplicates, unreadable or
    # service files) and the editions that compete for the same section are part of the completeness section.
    section_1_extra = {}
    if document_analysis:
        integrity = document_analysis.get("integrity") or {}
        revisions = document_analysis.get("revision_analysis") or {}
        excluded_by_stage = Counter(item.get("stage") for item in integrity.get("excluded") or [])
        for row in uploads:
            row["excluded_files"] = excluded_by_stage.get(row["stage"], 0)
            if row["excluded_files"]:
                row["comment"] = f"Исключено из сравнения: {row['excluded_files']} (дубликаты SHA-256, нечитаемые и служебные файлы)"
        section_1_extra = {
            "integrity": integrity,
            "revision_conflicts": revisions.get("conflicts") or [],
            "superseded_revisions": revisions.get("superseded_revisions") or [],
        }

    def statistic(name, count):
        return {"name": name, "count": count, "percent_of_matrix": round(100 * count / matrix_size, 2) if matrix_size else None}

    def check(row):
        result = evidence_group_to_submission_check(row)
        return {**result, "evidence_group_id": row["id"], "parameter_name": row["parameter"]["name"],
                "finding_status": row["finding_status"], "inspector_status": row["inspector_status"],
                "delta": row.get("delta"), "decisions": row.get("decisions", []),
                "evidence": row.get("fragments", [])}

    return {
        "schema_version": "case10-annex2-v1",
        "header": {**object_info, "signature_status": "UNSIGNED"},
        "section_1_document_upload": {"rows": uploads, "mixed_stage_files": stages["RD_ID_MIXED"], "unknown_stage_files": stages["UNKNOWN"], **section_1_extra},
        "section_2_statistics": [statistic("Всего параметров в Матрице", matrix_size), statistic("Параметров с результатом сравнения", len(checked)),
                                 statistic("Подтвержденных нарушений", len(confirmed)), statistic("Критических", len(critical)),
                                 statistic("Существенных", len(substantial)), statistic("Кандидатов на проверку", len(pending)),
                                 statistic("Подозрений ИИ", len(suspicions)), statistic("Параметров без результата сравнения", matrix_size - len(checked))],
        "section_3_unverified_parameters": [check(row) for row in missing],
        "section_4_critical_violations": [check(row) for row in critical],
        "section_5_substantial_violations": [check(row) for row in substantial],
        "section_6_ai_suspicions": [check(row) for row in suspicions],
        "section_7_resolution": {"status": "INSPECTOR_REQUIRED", "items": [
            {"parameter_code": row["parameter"]["code"], "evidence_group_id": row["id"],
             "inspector_comment": (row.get("decisions") or [{}])[-1].get("comment"),
             "recommendation": None} for row in confirmed]},
        "pending_candidates": [check(row) for row in pending],
    }


def render_protocol_pdf(protocol) -> bytes:
    import fitz

    payload = protocol.payload_json or {}
    annex = payload.get("annex_2") or {}
    labels = {
        "CANDIDATE": "Кандидат на нарушение",
        "CONFIRMED_VIOLATION": "Нарушение подтверждено",
        "NEGATIVE_VERIFIED": "Несоответствие не подтверждено",
        "MISSING_EVIDENCE": "Недостаточно доказательств",
        "NOT_APPLICABLE": "Неприменимо",
        "NOT_COMPARABLE": "Сравнение невозможно",
        "CLARIFICATION_REQUIRED": "Требуется уточнение",
        "SUSPICION": "Подозрение ИИ",
        "PENDING": "Ожидает решения",
        "CONFIRMED": "Подтверждено инспектором",
        "REJECTED": "Отклонено инспектором",
        "UNKNOWN": "Статус утверждения не указан",
        "PD_UPLOADED": "ПД загружена",
        "RD_UPLOADED": "РД загружена",
        "ID_UPLOADED": "ИД загружена",
        "PD_MISSING": "ПД отсутствует",
        "RD_MISSING": "РД отсутствует",
        "ID_MISSING": "ИД отсутствует",
        "no_relevant_evidence": "релевантные доказательства не найдены",
        "values_not_extracted": "значения не извлечены",
        "mixed_stage_requires_review": "смешанная стадия РД/ИД требует уточнения",
        "missing_stage": "отсутствует требуемая стадия",
        "missing_discipline_document": "на стадии нет документа нужного раздела",
        "evidence_discipline_mismatch": "найденное значение взято из раздела, не названного в каталоге",
        "revision_conflict": "конфликт редакций: несколько неупорядочиваемых версий раздела",
        "self_comparison": "ПД и РД опираются на один и тот же файл",
        "unit_mismatch": "рядом со значением иная единица измерения",
        "unit_evidence_absent": "рядом со значением нет единицы измерения параметра",
        "magnitude_mismatch": "значения на стадиях различаются на порядок и более",
        "multi_instance_parameter": "параметр задаётся для каждого элемента отдельно",
        "trigger_not_evaluable": "триггер нельзя проверить сравнением значений",
        "change_not_triggering": "изменение не удовлетворяет триггеру параметра",
        "MIXED_PROJECT_BASELINES": "смешаны серии шифров/поколения проекта",
        "UNORDERED_REVISIONS": "редакции нельзя упорядочить",
        "EXACT_DUPLICATE_WITHIN_STAGE": "точный дубль внутри стадии",
        "UNREADABLE_OR_EMPTY_SOURCE_FILE": "файл пуст или не читается",
        "TEMPORARY_OR_SERVICE_FILE": "служебный/временный файл",
        "EXCLUDED_BY_MANIFEST": "исключён по манифесту",
    }

    def text(value):
        return escape(str(value if value is not None else "Не указано"))

    def label(value):
        return labels.get(value, value)

    def table(headers, rows):
        if not rows:
            return "<p>Нет записей.</p>"
        return "<table><tr>" + "".join(f"<th>{text(h)}</th>" for h in headers) + "</tr>" + "".join(
            "<tr>" + "".join(f"<td>{text(cell)}</td>" for cell in row) + "</tr>" for row in rows) + "</table>"

    def checks(rows):
        if not rows:
            return "<p>Нет записей.</p>"
        parts = []
        for row in rows:
            parts.append(f"<h3>{text(row.get('parameter_code'))} {text(row.get('parameter_name'))}</h3>")
            parts.append(f"<p>{text(label(row.get('finding_status')))}; решение: {text(label(row.get('inspector_status')))}</p>")
            result = (row.get("delta") or {}).get("reason") or row.get("protocol_status")
            parts.append(table(["ПД", "РД", "ИД", "Результат"], [[row.get("pd_value"), row.get("rd_value"), row.get("id_value"), label(result)]]))
            for source in row.get("evidence") or []:
                parts.append(f"<p class='source'>{text(source.get('file_id') or source.get('file'))}, {text(source.get('dataset_stage') or source.get('stage'))}, стр. {text(source.get('page'))}; редакция {text(source.get('revision'))}; {text(label(source.get('approval_status')))}; bbox {text(source.get('bbox_normalized'))}</p>")
            for decision in row.get("decisions") or []:
                parts.append(f"<p>Инспектор {text(decision.get('user_id'))}, {text(decision.get('created_at'))}: {text(label(decision.get('decision')))}. Причина: {text(label(decision.get('reason_code')))}. {text(decision.get('comment'))}</p>")
        return "".join(parts)

    header = annex.get("header", {})
    html = [f"<h1>ПРОТОКОЛ АВТОМАТИЗИРОВАННОЙ СВЕРКИ № {protocol.id}</h1>",
            f"<p>Объект: {text(header.get('object_name') or protocol.object_id)}<br>Адрес: {text(header.get('address'))}<br>Номер надзорного дела: {text(header.get('supervisory_case_number'))}<br>Застройщик: {text(header.get('developer'))}<br>Подрядчик: {text(header.get('contractor'))}<br>Версия: {protocol.version}. Статус: {text(protocol.status)}<br>Дата: {text(protocol.created_at)}<br>Матрица: {text(protocol.matrix_version)}</p>",
            "<p>Электронная подпись не наложена. Финализация фиксирует неизменяемую версию в системе.</p>",
            "<h2>РАЗДЕЛ 1. СТАТУС ЗАГРУЗКИ ДОКУМЕНТОВ</h2>",
            table(["Тип", "Статус", "Загружено", "Ожидается"], [[r["stage"], label(r["status"]), r["loaded_files"], r["expected_files"]] for r in annex.get("section_1_document_upload", {}).get("rows", [])]),
            f"<p>Смешанная стадия РД/ИД: {annex.get('section_1_document_upload', {}).get('mixed_stage_files', 0)}</p>",
            "<h3>Исключённые из сравнения файлы</h3>",
            table(["Файл", "Стадия", "Причина", "Дубль файла"], [[i.get("file_id"), i.get("stage"), label(i.get("reason")), i.get("duplicate_of")] for i in (annex.get("section_1_document_upload", {}).get("integrity") or {}).get("excluded", [])]),
            "<h3>Конфликты редакций</h3>",
            table(["Стадия", "Раздел", "Тип", "Файлы"], [[c.get("stage"), c.get("scope"), label(c.get("type")), ", ".join(c.get("file_ids") or [])] for c in annex.get("section_1_document_upload", {}).get("revision_conflicts", [])]),
            *([f"<p>{text((payload.get('live_tagger_coverage') or {}).get('summary'))}</p>"] if payload.get("live_tagger_coverage") else []),
            "<h2>РАЗДЕЛ 2. СВОДНАЯ СТАТИСТИКА</h2>",
            table(["Показатель", "Количество", "% от Матрицы"], [[r["name"], r["count"], r["percent_of_matrix"]] for r in annex.get("section_2_statistics", [])])]
    for key, title in (("section_3_unverified_parameters", "3. НЕПРОВЕРЕННЫЕ ПАРАМЕТРЫ"),
                       ("section_4_critical_violations", "4. КРИТИЧЕСКИЕ НАРУШЕНИЯ"),
                       ("section_5_substantial_violations", "5. СУЩЕСТВЕННЫЕ НАРУШЕНИЯ"),
                       ("section_6_ai_suspicions", "6. ПОДОЗРЕНИЯ ИИ")):
        html += [f"<h2>РАЗДЕЛ {title}</h2>", checks(annex.get(key, []))]
    html += ["<h2>РАЗДЕЛ 7. РЕЗОЛЮТИВНАЯ ЧАСТЬ</h2>",
             table(["Параметр", "Комментарий инспектора", "Рекомендация"], [[r["parameter_code"], r["inspector_comment"], r["recommendation"]] for r in annex.get("section_7_resolution", {}).get("items", [])]),
             "<h2>КАНДИДАТЫ, ОЖИДАЮЩИЕ ПРОВЕРКИ</h2>", checks(annex.get("pending_candidates", []))]
    buffer = BytesIO()
    writer = fitz.DocumentWriter(buffer)
    story = fitz.Story(html="".join(html), user_css="body {font-family:serif; font-size:10pt;} h1 {font-size:16pt;} h2 {font-size:12pt;page-break-before:always;} h3 {font-size:10pt;} table {border-collapse:collapse;width:100%;} td,th {border:0.5pt solid #999;padding:4pt;} .source {font-size:8pt;}")
    story.write(writer, lambda number, filled: (fitz.Rect(0, 0, 595, 842), fitz.Rect(36, 36, 559, 800), None))
    writer.close()
    with fitz.open(stream=buffer.getvalue(), filetype="pdf") as pdf:
        total = len(pdf)
        for number, page in enumerate(pdf, start=1):
            page.insert_text((285, 824), f"{number} / {total}", fontsize=8, color=(0.25, 0.25, 0.25))
        return pdf.tobytes(garbage=3, deflate=True)
