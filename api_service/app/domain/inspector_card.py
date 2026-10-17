"""Final / step C1: the inspector card brought to the experts' checklist (МГСУ.pdf, pp. 4-11).

Everything here is additive to `inspector_workbench` (queue, card, panels, edits, bulk decisions, timing) and is
exposed through `workbench_summary_full` / `workbench_group_full`, which wrap the S5 payloads with:

  * `sheet`          the sheet designation read from the page's title block (text layer), or None -- never invented;
  * `stage_info`     where a panel's stage comes from (document / registry / file name) and a warning when they
                     disagree (the experts' reference labelled an "Исполнительный чертеж" panel as РД);
  * `no_result`      one of six honest "no result" states with a reason and the action that is available;
  * `parts`          the parts of a compound observation and the inspector's per-part decisions (stored as
                     append-only `InspectorEdit` rows, entity_type PART_DECISION -- no schema change);
  * `explanation`    {rule, threshold, threshold_basis, deviation_abs, deviation_pct, unit, text_confidence,
                     link_confidence, probability}: only what the pipeline actually computed; a missing field is
                     None ("не вычислялось"), and a violation probability is never produced;
  * `hypothesis`     the ТЗ 9.5 card of a free-search hypothesis;
  * `progress_panel` documents/pages processed, skips with reasons, found/reviewed candidates, incomplete flag;
  * `protocol_entry` the record of one finding as it goes into the protocol (source, values, status, decision,
                     rationale), for the "go to protocol entry / export" actions.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..db.models import DocumentVersion, EvidenceGroup, InspectionProcess, InspectorEdit, Suspicion
from . import inspector_workbench as wb
from .live_candidate_tagger import live_tagger_coverage
from .v3_pipeline import (
    DECISION_TO_STATUS,
    _project_documents,
    current_document_versions,
    evidence_group_to_dict,
    latest_protocol,
    record_inspector_decision,
)
from evaluation.exporter import evidence_group_to_submission_check

ENTITY_PART = "PART_DECISION"
ENTITY_HYPOTHESIS = "HYPOTHESIS_REVIEW"
HYPOTHESIS_STATUSES = ("CONFIRMED", "REJECTED", "CLARIFICATION_REQUIRED")  # same vocabulary as the inspector's decision on a finding
PART_ACTIONS = {"Confirm": "CONFIRM", "Reject": "REJECT", "Clarification Required": "CLARIFY"}
PART_ACTION_TO_DECISION = {value: key for key, value in PART_ACTIONS.items()}

# ---------------------------------------------------------------------------------------------------------------
# sheet designation (title block of the page)
# ---------------------------------------------------------------------------------------------------------------
_CIPHER_RE = re.compile(r"^[A-Za-zА-Яа-я0-9]{2,}[-/._][A-Za-zА-Яа-я0-9._/\-]{4,}$")
_TITLE_BLOCK_X = 0.5   # the title block (основная надпись, ГОСТ Р 21.101) sits in the lower-right corner of the sheet
_TITLE_BLOCK_Y = 0.72


def _visible_words(page) -> list[tuple[float, float, float, float, str]]:
    """Words of the text layer in the visible (rotated) frame the viewer and `bbox_norm` use."""
    import fitz

    matrix = page.rotation_matrix
    rotated = bool(page.rotation)
    out = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        rect = fitz.Rect(x0, y0, x1, y1) * matrix if rotated else fitz.Rect(x0, y0, x1, y1)
        rect.normalize()
        out.append((rect.x0, rect.y0, rect.x1, rect.y1, str(text)))
    return out


def extract_sheet_designation(words: list[tuple[float, float, float, float, str]], width: float, height: float) -> dict[str, Any] | None:
    """The sheet number (the figure under the "Лист" header that stands next to "Листов") and the document cipher
    printed in the title block. Both come from the page's own text layer; a page without a recognisable title block
    (a table page, a scan) returns None and the card shows "—"."""
    if width <= 0 or height <= 0:
        return None
    region = [w for w in words if w[0] > width * _TITLE_BLOCK_X and w[3] > height * _TITLE_BLOCK_Y]
    low = lambda w: w[4].lower().strip(":. ")  # noqa: E731
    headers = [w for w in region if low(w) == "лист"]
    totals = [w for w in region if low(w) == "листов"]
    sheet_no = None
    for header in headers:
        beside = [t for t in totals if abs(t[1] - header[1]) <= height * 0.012 and 0 < t[0] - header[0] < width * 0.06]
        if not beside:
            continue  # the "Лист" of the revision table (Изм. Кол.уч. Лист № док.) has no "Листов" next to it
        centre = (header[0] + header[2]) / 2
        tolerance = max(header[2] - header[0], width * 0.012)
        below = [w for w in region if w[4].isdigit() and len(w[4]) <= 4 and header[3] <= w[1] <= header[3] + height * 0.05
                 and abs((w[0] + w[2]) / 2 - centre) <= tolerance]
        if below:
            sheet_no = min(below, key=lambda w: w[1])[4]
            break
    ciphers = [w[4] for w in region if _CIPHER_RE.match(w[4]) and any(c.isdigit() for c in w[4]) and any(c.isalpha() for c in w[4])]
    cipher = max(ciphers, key=len) if ciphers else None
    if not sheet_no and not cipher:
        return None
    parts = []
    if sheet_no:
        parts.append(f"Лист {sheet_no}")
    if cipher:
        parts.append(cipher)
    return {"sheet": sheet_no, "cipher": cipher, "label": " · ".join(parts), "basis": "основная надпись листа (текстовый слой)"}


@lru_cache(maxsize=1024)
def _sheet_cached(key: tuple[int, str], page: int, loader: Any) -> dict[str, Any] | None:
    import fitz

    with fitz.open(stream=loader(), filetype="pdf") as pdf:
        if page < 1 or page > len(pdf):
            return None
        target = pdf[page - 1]
        return extract_sheet_designation(_visible_words(target), float(target.rect.width), float(target.rect.height))


def sheet_designation(document: DocumentVersion | None, page: object) -> dict[str, Any] | None:
    if document is None or not page:
        return None
    try:
        found = _sheet_cached(wb._document_cache_key(document), int(page), wb._Loader(document))
    except (FileNotFoundError, ValueError, KeyError, RuntimeError, OSError):
        return None
    return dict(found) if found else None


# ---------------------------------------------------------------------------------------------------------------
# stage provenance
# ---------------------------------------------------------------------------------------------------------------
_STAGE_FROM_DOC = {"project": "PD", "working": "RD", "as_built": "ID"}
_FILE_MARKERS = (
    ("ID", re.compile(r"(?<![А-Яа-яA-Za-z])ИД(?![А-Яа-яA-Za-z])|исполнительн", re.IGNORECASE)),
    ("RD", re.compile(r"(?<![А-Яа-яA-Za-z])РД(?![А-Яа-яA-Za-z])|рабоч", re.IGNORECASE)),
    ("PD", re.compile(r"(?<![А-Яа-яA-Za-z])ПД(?![А-Яа-яA-Za-z])|проектн", re.IGNORECASE)),
)


def stage_info(document: DocumentVersion | None, panel_stage: str) -> dict[str, Any]:
    """Where the stage of a panel comes from, and a warning if the sources disagree. The panel is labelled by the
    document's own stage (registry / classification), never by what the file name says; the name is only compared."""
    if document is None:
        return {"panel_stage": panel_stage, "sources": [], "conflict": False, "warning": None}
    sources = []
    registry = str(document.dataset_stage or "").upper() or None
    declared = _STAGE_FROM_DOC.get(str(document.doc_stage or ""))
    if registry in wb.STAGES:
        sources.append({"basis": "реестр файлов", "stage": registry})
    if declared:
        sources.append({"basis": "классификация документа", "stage": declared})
    name = str(document.filename or "")
    markers = sorted({stage for stage, pattern in _FILE_MARKERS if pattern.search(name)}, key=wb.STAGES.index)
    if markers:
        sources.append({"basis": "имя файла", "stage": "/".join(markers), "stage_label": "/".join(wb_stage_title(x) for x in markers)})
    elif not sources:
        sources.append({"basis": "не определена: нет записи в реестре и отметки в имени файла", "stage": None})
    document_stages = {s["stage"] for s in sources if s["basis"] != "имя файла" and s["stage"]}
    conflicts = []
    if document_stages and document_stages != {panel_stage}:
        conflicts.append(f"панель {wb_stage_title(panel_stage)}, но по {', '.join(s['basis'] for s in sources if s['stage'] in document_stages)} "
                         f"стадия — {'/'.join(sorted(document_stages))}")
    if markers and panel_stage not in markers:
        conflicts.append(f"в имени файла отмечено {'/'.join(wb_stage_title(x) for x in markers)}, панель — {wb_stage_title(panel_stage)}")
    return {"panel_stage": panel_stage, "sources": sources, "conflict": bool(conflicts), "warning": "; ".join(conflicts) or None}


def wb_stage_title(stage: str) -> str:
    return {"PD": "ПД", "RD": "РД", "ID": "ИД"}.get(stage, stage)


# ---------------------------------------------------------------------------------------------------------------
# the six "no result" states
# ---------------------------------------------------------------------------------------------------------------
NO_DOCUMENTS = "NO_DOCUMENTS"
NO_VALUE = "NO_VALUE_IN_DOCUMENT"
NOT_COMPARABLE = "NOT_COMPARABLE"
PROCESSING_ERROR = "PROCESSING_ERROR"
INCOMPLETE = "INCOMPLETE_CHECK"
NO_DIFFERENCE = "NO_DIFFERENCE"

STATE_TITLES = {
    NO_DOCUMENTS: "Нет документов",
    NO_VALUE: "Нет значения в документе",
    NOT_COMPARABLE: "Сравнение невозможно",
    PROCESSING_ERROR: "Ошибка обработки",
    INCOMPLETE: "Проверка не завершена",
    NO_DIFFERENCE: "Расхождения нет",
}
_REASON_TEXT = {
    "revision_conflict": "в стадии несколько конкурирующих редакций документа — выберите актуальную на вкладке «Редакции»",
    "revision_clarification_required": "редакцию документа нужно уточнить на вкладке «Редакции»",
    "registry_clarification_required": "в реестре файлов стадия или редакция документа неоднозначна",
    "missing_discipline_document": "в загруженной стадии нет документа нужного раздела",
    "missing_stage": "нет документов одной из сравниваемых стадий",
    "mixed_stage_requires_review": "файл содержит и РД, и ИД — стадию нужно подтвердить по реестру",
    "no_approved_current_revision": "нет утверждённой действующей редакции документа",
    "no_matching_entity_type": "в документах нет объекта этого типа для сравнения",
    "value_not_extracted": "значение параметра в документе не найдено",
    "garbled_table_text": "текстовый слой таблицы повреждён — значение не прочитано",
    "broken_text_layer_no_ocr": "у страницы нет читаемого текстового слоя, а распознавание не включено",
    "evidence_no_longer_found": "после пересчёта прежнее доказательство не найдено — проверьте повторно",
    "evidence_basis_changed": "документы изменились — проверьте карточку повторно",
    "attempts_exhausted": "обработка остановилась после исчерпания попыток",
    "worker_recovery": "обработка прервана и восстановлена — результат нужно проверить",
}
_PROCESSING_REASONS = {"garbled_table_text", "broken_text_layer_no_ocr", "attempts_exhausted", "worker_recovery", "LOW_QUALITY"}


def _reason_of(delta: dict[str, Any]) -> str:
    gate = delta.get("gate") if isinstance(delta.get("gate"), dict) else {}
    return str(delta.get("reason") or gate.get("reason") or "")


def _clean_trigger_text(text: object) -> str | None:
    """The catalog trigger without the import metadata glued in front of it (`parameter_id=..;matrix_row=..;trigger=`)."""
    value = str(text or "").strip()
    if not value:
        return None
    return value.split("trigger=")[-1].strip() or None


def _needed_stage(delta: dict[str, Any], visible: list[str]) -> str | None:
    missing = [s for s in delta.get("missing_stages") or [] if s in wb.STAGES]
    if missing:
        return missing[0]
    if _reason_of(delta) == "missing_stage":  # the annotation tier lists the absent stage(s) under `stages`
        absent = [s for s in delta.get("stages") or [] if s in wb.STAGES]
        if absent:
            return absent[0]
    gate = delta.get("gate") if isinstance(delta.get("gate"), dict) else {}
    stage = gate.get("stage")
    if stage in wb.STAGES:
        return stage
    for candidate in ("RD", "PD"):
        if candidate not in visible:
            return candidate
    return None


def no_result_state(*, finding_status: str, comparability_status: str | None, delta: dict[str, Any], visible_stages: list[str],
                    low_quality: bool, has_fragments: bool, inspector_status: str = "PENDING",
                    pages: dict[str, list[int]] | None = None) -> dict[str, Any] | None:
    """None for a candidate / confirmed violation / hypothesis (they have a result to judge)."""
    status = str(finding_status or "")
    if status in {"CANDIDATE", "CONFIRMED_VIOLATION", "SUSPICION"}:
        return None
    reason = _reason_of(delta)
    reason_text = _REASON_TEXT.get(reason)
    stages_text = ", ".join(wb_stage_title(s) for s in visible_stages) or "—"
    missing = [s for s in delta.get("missing_stages") or [] if s in wb.STAGES]

    if status == "NEGATIVE_VERIFIED":
        checked = "; ".join(f"{wb_stage_title(stage)} с. {', '.join(str(p) for p in sorted(set(nums)))}" for stage, nums in (pages or {}).items() if nums)
        scope = f"Сравнены только указанные в карточке страницы ({checked}); остальные страницы документов этим параметром не просматривались." if checked \
            else "Сравнение выполнено по найденным в карточке фрагментам; остальные страницы этим параметром не просматривались."
        trigger_reason = "значения различаются, но изменение не удовлетворяет условию триггера" if reason == "change_not_triggering" else "значения совпадают"
        return {"state": NO_DIFFERENCE, "title": STATE_TITLES[NO_DIFFERENCE], "reason": f"{trigger_reason}. {scope}",
                "action": {"kind": "none", "label": "Отметка о соответствии относится только к просмотренным фрагментам"},
                "decidable": False}
    if low_quality or reason in _PROCESSING_REASONS:
        return {"state": PROCESSING_ERROR, "title": STATE_TITLES[PROCESSING_ERROR],
                "reason": reason_text or "страница признана нечитаемой (LOW_QUALITY): значение не извлечено",
                "action": {"kind": "edit_evidence", "label": "Указать значение и область вручную («Добавить фрагмент») или дозагрузить читаемую копию"},
                "decidable": False}
    if status == "MISSING_EVIDENCE":
        if reason == "missing_discipline_document" or reason == "missing_stage" or (missing and not has_fragments):
            need = _needed_stage(delta, visible_stages)
            return {"state": NO_DOCUMENTS, "title": STATE_TITLES[NO_DOCUMENTS],
                    "reason": (reason_text or "для сравнения не хватает документа") + (f" (стадия {wb_stage_title(need)}, есть: {stages_text})" if need else ""),
                    "action": {"kind": "upload", "stage": need, "label": f"Дозагрузить документы стадии {wb_stage_title(need)}" if need else "Дозагрузить недостающие документы"},
                    "decidable": False}
        return {"state": NO_VALUE, "title": STATE_TITLES[NO_VALUE],
                "reason": reason_text or "документы загружены, но значение параметра в них не найдено",
                "action": {"kind": "edit_evidence", "label": "Найти значение на листе и добавить фрагмент, либо оставить параметр без результата"},
                "decidable": False}
    if status == "NOT_COMPARABLE" or (comparability_status and comparability_status not in ("COMPARABLE", "PARTIAL")):
        action = {"kind": "revisions", "label": "Выбрать актуальную редакцию на вкладке «Редакции»"} if reason.startswith("revision") or reason.startswith("registry") \
            else {"kind": "edit_evidence", "label": "Проверить источники фрагментов и при необходимости исправить их вручную"}
        return {"state": NOT_COMPARABLE, "title": STATE_TITLES[NOT_COMPARABLE],
                "reason": reason_text or "значения нельзя сопоставить: разные объект, единицы или смысл параметра",
                "action": action, "decidable": False}
    if status == "CLARIFICATION_REQUIRED":
        return {"state": INCOMPLETE, "title": STATE_TITLES[INCOMPLETE],
                "reason": reason_text or "проверка этого параметра не завершена: нужно уточнение инспектора",
                "action": {"kind": "decide", "label": "Принять решение (подтвердить / отклонить / уточнить) после просмотра листов"},
                "decidable": True}
    return {"state": INCOMPLETE, "title": STATE_TITLES[INCOMPLETE], "reason": reason_text or f"статус {status}",
            "action": {"kind": "none", "label": "—"}, "decidable": False}


# ---------------------------------------------------------------------------------------------------------------
# compound observations and per-part decisions
# ---------------------------------------------------------------------------------------------------------------
_SYSTEM_TOKEN = re.compile(r"^[A-Za-zА-Яа-я]{1,3}\d+(?:[.\-]\d+)*$")


def _tokens(value: object) -> list[str]:
    return [t.strip() for t in re.split(r"[,;]", str(value or "")) if t.strip()]


def compound_parts(delta: dict[str, Any]) -> list[dict[str, Any]]:
    """Parts of a compound observation: the components the compound extractor compared, or the individual systems
    of a "set of systems" comparison (ПД {V2.7, V2.8} against РД {V2.7}); [] for a single-valued observation."""
    components = delta.get("components")
    if isinstance(components, list) and len(components) >= 2:
        return [{"key": f"c{index}", "label": str(c.get("label") or f"компонент {index + 1}"), "expected": c.get("expected"),
                 "actual": c.get("actual"), "differs": not bool(c.get("equal")),
                 "state": "совпадает" if c.get("equal") else "различается"} for index, c in enumerate(components)]
    values = delta.get("values") if isinstance(delta.get("values"), dict) else {}
    pd_value = values.get("PD")
    later_stage = "RD" if values.get("RD") is not None else "ID" if values.get("ID") is not None else None
    if pd_value is None or later_stage is None:
        return []
    pd_tokens = _tokens(pd_value)
    later_raw = str(values.get(later_stage) or "")
    later_tokens = [] if later_raw.upper().startswith("MISSING") else _tokens(later_raw)  # "MISSING_*" is the extractor's own "nothing found"
    if not pd_tokens or not all(_SYSTEM_TOKEN.match(t) for t in [*pd_tokens, *later_tokens]):
        return []
    union = list(dict.fromkeys([*pd_tokens, *later_tokens]))
    if len(union) < 2:
        return []
    out = []
    for token in union:
        in_pd, in_later = token in pd_tokens, token in later_tokens
        state = "есть в обеих стадиях" if in_pd and in_later else (f"пропала в {wb_stage_title(later_stage)}" if in_pd else f"добавлена в {wb_stage_title(later_stage)}")
        out.append({"key": f"s:{token}", "label": token, "expected": token if in_pd else None, "actual": token if in_later else None,
                    "differs": in_pd != in_later, "state": state})
    return out


def _latest_part_edits(db: Session, group_id: int) -> dict[str, InspectorEdit]:
    rows = (db.query(InspectorEdit)
            .filter(InspectorEdit.evidence_group_id == int(group_id), InspectorEdit.entity_type == ENTITY_PART)
            .order_by(InspectorEdit.id.asc()).all())
    latest: dict[str, InspectorEdit] = {}
    for row in rows:
        latest[row.entity_key] = row
    return latest


def part_decisions(db: Session, group: EvidenceGroup) -> dict[str, Any]:
    delta = group.delta if isinstance(group.delta, dict) else {}
    parts = compound_parts(delta)
    latest = _latest_part_edits(db, int(group.id))
    rows = []
    for part in parts:
        edit = latest.get(f"part:{int(group.id)}:{part['key']}")
        value = (edit.new_value or {}) if edit else {}
        rows.append({**part, "decision": PART_ACTION_TO_DECISION.get(edit.action) if edit else None, "comment": value.get("comment"),
                     "version": int(edit.version) if edit else 0, "decided_at": edit.created_at if edit else None,
                     "user_id": edit.user_id if edit else None})
    decided = sum(1 for r in rows if r["decision"])
    return {"compound": bool(parts), "parts": rows, "decided": decided, "total": len(rows),
            "partial": 0 < decided < len(rows), "complete": bool(rows) and decided == len(rows)}


def record_part_decisions(db: Session, group: EvidenceGroup, *, items: list[dict[str, Any]], comment: object, user_id: int | None) -> dict[str, Any]:
    """Per-part decisions on a compound finding ("Разделить на части"). Each part is a new version of an
    append-only `InspectorEdit` (reason mandatory for a rejected / clarified part). When every part has a decision
    the group gets one overall decision through the normal path (so the list, card and protocol agree): Confirm if
    at least one part is confirmed, else Reject if all are rejected, else Clarification Required."""
    process = wb._group_process(db, group)
    wb.require_open_process(process)
    delta = group.delta if isinstance(group.delta, dict) else {}
    known = {p["key"]: p for p in compound_parts(delta)}
    if not known:
        raise HTTPException(status_code=422, detail="This finding is not a compound observation")
    if not items:
        raise HTTPException(status_code=422, detail="No parts given")
    seen: set[str] = set()
    for item in items:
        key = str(item.get("key") or "")
        if key not in known or key in seen:
            raise HTTPException(status_code=422, detail=f"Unknown or repeated part: {key}")
        seen.add(key)
        if item.get("decision") not in PART_ACTIONS:
            raise HTTPException(status_code=422, detail="Unsupported part decision")
        if item["decision"] != "Confirm":
            wb.require_reason(item.get("comment"), what=f"comment for part «{known[key]['label']}»")
    recorded = []
    for item in items:
        part = known[item["key"]]
        reason = str(item.get("comment") or "").strip() or "Подтверждено инспектором"
        edit_key = f"part:{int(group.id)}:{part['key']}"
        _, previous = wb._next_version(db, str(process.id), edit_key)
        edit = wb._record_edit(
            db, process=process, group=group, entity_type=ENTITY_PART, entity_key=edit_key,
            action=PART_ACTIONS[item["decision"]], previous_value=previous.new_value if previous else None,
            new_value={"part_key": part["key"], "label": part["label"], "decision": item["decision"], "state": part["state"],
                       "comment": str(item.get("comment") or "").strip() or None},
            reason=reason, user_id=user_id, audit_action="PART_DECISION_RECORDED")
        recorded.append(edit)
    db.flush()
    db.refresh(group)
    summary = part_decisions(db, group)
    group_decision = None
    if summary["complete"]:
        decisions = [r["decision"] for r in summary["parts"]]
        overall = "Confirm" if "Confirm" in decisions else "Reject" if all(d == "Reject" for d in decisions) else "Clarification Required"
        by = {d: [r["label"] for r in summary["parts"] if r["decision"] == d] for d in set(decisions)}
        text = "Составное замечание, решение по частям: " + "; ".join(
            f"{ {'Confirm': 'подтверждено', 'Reject': 'отклонено', 'Clarification Required': 'уточнить'}[d] }: {', '.join(labels)}" for d, labels in by.items())
        extra = str(comment or "").strip()
        group_decision = record_inspector_decision(
            db, evidence_group_id=int(group.id), decision=overall,
            reason_code="NOT_A_VIOLATION" if overall == "Reject" else None,
            comment=f"{text}. {extra}" if extra else text, user_id=user_id)
    else:
        db.commit()
    return {"parts": part_decisions(db, db.get(EvidenceGroup, int(group.id))), "group_decision": (
        {"id": int(group_decision.id), "decision": group_decision.decision, "finding_status": DECISION_TO_STATUS[group_decision.decision]}
        if group_decision else None)}


# ---------------------------------------------------------------------------------------------------------------
# explanation of the discrepancy
# ---------------------------------------------------------------------------------------------------------------
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def _single_number(value: object) -> float | None:
    found = _NUMBER.findall(str(value or ""))
    if len(found) != 1:
        return None
    try:
        return float(found[0].replace(",", "."))
    except ValueError:
        return None


def explanation_block(*, delta: dict[str, Any], param: Any, expected: object, actual: object, confidence: float | None,
                      rule_version: str | None) -> dict[str, Any]:
    """The "Основание" block. A field the pipeline did not compute stays None; the UI prints "не вычислялось".
    `probability` is deliberately always None: no verifiable method of computing a violation probability exists."""
    trigger = delta.get("trigger") if isinstance(delta.get("trigger"), dict) else {}
    unit = getattr(param, "unit", None)
    threshold = None
    threshold_text = None
    if trigger.get("threshold") is not None:
        threshold = trigger.get("threshold")
        threshold_text = f"{trigger.get('label') or ''}".strip() or f"{threshold} {trigger.get('threshold_unit') or ''}".strip()
    elif trigger.get("label"):
        threshold_text = str(trigger["label"])
    dev_abs = trigger.get("change")
    dev_pct = trigger.get("change_pct")
    computed_by = "правило триггера" if dev_abs is not None else None
    if dev_abs is None:
        left, right = _single_number(expected), _single_number(actual)
        if left is not None and right is not None:
            dev_abs = round(right - left, 6)
            dev_pct = round(abs(right - left) / abs(left) * 100, 2) if left else None
            computed_by = "разность извлечённых значений"
    trigger_logic = _clean_trigger_text(getattr(param, "trigger_logic", None))
    basis_parts = []
    if trigger_logic:
        basis_parts.append(f"Каталог параметров, триггер: «{trigger_logic}»")
    for attr, title in (("sp_reference", "СП"), ("gost_reference", "ГОСТ"), ("fz_reference", "ФЗ")):
        if getattr(param, attr, None):
            basis_parts.append(f"{title}: {getattr(param, attr)}")
    return {
        "rule": trigger.get("label") or rule_version,
        "rule_kind": trigger.get("kind"),
        "rule_evaluation": trigger.get("evaluation"),
        "condition": trigger_logic,
        "threshold": threshold if threshold is not None else threshold_text,
        "threshold_unit": trigger.get("threshold_unit") or (unit if threshold is not None else None),
        "threshold_text": threshold_text,
        "threshold_basis": "; ".join(basis_parts) or None,
        "deviation_abs": dev_abs,
        "deviation_pct": dev_pct,
        "deviation_basis": computed_by,
        "unit": unit,
        "needs_review": bool(trigger.get("needs_review")),
        "text_confidence": delta.get("text_confidence"),
        "link_confidence": delta.get("link_confidence"),
        "combined_confidence": confidence,
        "similarity": delta.get("similarity"),
        "probability": None,
        "probability_note": "Вероятность нарушения не заявляется: способа её расчёта в системе нет.",
    }


# ---------------------------------------------------------------------------------------------------------------
# hypothesis card (ТЗ 9.5)
# ---------------------------------------------------------------------------------------------------------------
def is_hypothesis(group: EvidenceGroup) -> bool:
    delta = group.delta if isinstance(group.delta, dict) else {}
    return group.finding_status == "SUSPICION" or delta.get("matrix_scope") == "FREE_SEARCH"


def hypothesis_review_status(group: EvidenceGroup) -> str:
    row = group.suspicion
    return str(row.inspector_status or "PENDING") if row is not None else "PENDING"


def hypothesis_history(db: Session, group: EvidenceGroup) -> list[dict[str, Any]]:
    rows = (db.query(InspectorEdit)
            .filter(InspectorEdit.evidence_group_id == int(group.id), InspectorEdit.entity_type == ENTITY_HYPOTHESIS)
            .order_by(InspectorEdit.id.asc()).all())
    return [wb.edit_to_dict(row) for row in rows]


def record_hypothesis_review(db: Session, group: EvidenceGroup, *, status: str, comment: object, user_id: int | None) -> dict[str, Any]:
    """The inspector's review of a free-search hypothesis (TZ 9.5 "status of review"): confirmed / rejected /
    clarification required, with a mandatory reason. The status lives on the hypothesis row (`suspicions`); every
    change is also an append-only `InspectorEdit` (author, time, reason, previous status). A hypothesis is not a
    matrix finding, so this does not touch the finding status or the decision log of findings."""
    process = wb._group_process(db, group)
    wb.require_open_process(process)
    if not is_hypothesis(group):
        raise HTTPException(status_code=422, detail="This evidence group is not a free-search hypothesis")
    if status not in HYPOTHESIS_STATUSES:
        raise HTTPException(status_code=422, detail="Unsupported hypothesis review status")
    reason = wb.require_reason(comment, what="review rationale")
    delta = group.delta if isinstance(group.delta, dict) else {}
    row = group.suspicion
    if row is None:
        row = Suspicion(evidence_group_id=int(group.id), process_id=str(group.process_id), project_id=int(group.project_id),
                        organization_id=int(group.organization_id), object_id=group.object_id,
                        discovery_method=str(delta.get("discovery_method") or delta.get("source") or "UNKNOWN"),
                        confidence=group.confidence, description=str(delta.get("description") or group.expected_value or ""))
        db.add(row)
        db.flush()
    previous = str(row.inspector_status or "PENDING")
    row.inspector_status = status
    db.add(row)
    wb._record_edit(
        db, process=process, group=group, entity_type=ENTITY_HYPOTHESIS, entity_key=f"hypothesis:{int(group.id)}", action=status,
        previous_value={"status": previous}, new_value={"status": status, "comment": reason}, reason=reason,
        user_id=user_id, audit_action="HYPOTHESIS_REVIEWED")
    db.commit()
    db.refresh(group)
    return {"status": status, "previous_status": previous, "history": hypothesis_history(db, group)}


def hypothesis_card(group: EvidenceGroup, delta: dict[str, Any], sources: dict[str, Any], inspector_status: str,
                    history: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    if not is_hypothesis(group):
        return None
    inspector_status = hypothesis_review_status(group)
    param = group.param
    normative = [str(v) for v in (delta.get("normative_base"), getattr(param, "sp_reference", None), getattr(param, "gost_reference", None),
                                  getattr(param, "fz_reference", None)) if v]
    return {
        "method": delta.get("discovery_method") or delta.get("source"),
        "method_detail": delta.get("rule_name") or delta.get("rule_id"),
        "confidence": group.confidence,
        "description": delta.get("description") or group.expected_value,
        "pd_reference": delta.get("pd_reference") or sources.get("expected"),
        "rd_reference": delta.get("rd_reference") or sources.get("actual"),
        "criticality": delta.get("criticality") or group.review_priority,
        "normative_basis": "; ".join(dict.fromkeys(normative)) or None,
        "review_status": inspector_status,
        "review_history": history or [],
        "finding_status": group.finding_status,
        "note": "Гипотеза не является найденным нарушением; она проходит рассмотрение отдельно от кандидатов матрицы.",
    }


# ---------------------------------------------------------------------------------------------------------------
# payload wrappers
# ---------------------------------------------------------------------------------------------------------------
def workbench_group_full(db: Session, group: EvidenceGroup) -> dict[str, Any]:
    payload = wb.workbench_group(db, group)
    delta = group.delta if isinstance(group.delta, dict) else {}
    documents: dict[int, DocumentVersion | None] = {}

    def doc(doc_id: object) -> DocumentVersion | None:
        if not doc_id:
            return None
        key = int(doc_id)
        if key not in documents:
            documents[key] = db.get(DocumentVersion, key)
        return documents[key]

    pages: dict[str, list[int]] = defaultdict(list)
    for item in payload["effective_fragments"]:
        current = item.get("current") or {}
        document = doc(current.get("document_version_id"))
        item["sheet"] = sheet_designation(document, current.get("page"))
        item["stage_info"] = stage_info(document, item["stage"])
        if item["status"] == wb.FRAGMENT_ACTIVE and current.get("page"):
            pages[item["stage"]].append(int(current["page"]))
    payload["panels"] = {stage: [item for item in payload["effective_fragments"] if item["stage"] == stage] for stage in wb.STAGES}
    card = payload["card"]
    card["sheets"] = {stage: next((i["sheet"] for i in payload["panels"][stage] if i["status"] == wb.FRAGMENT_ACTIVE and i.get("sheet")), None)
                      for stage in wb.STAGES}
    quality = payload["quality"]
    payload["no_result"] = no_result_state(
        finding_status=group.finding_status, comparability_status=group.comparability_status, delta=delta,
        visible_stages=payload["visible_stages"], low_quality=bool(quality.get("low_quality")),
        has_fragments=bool(payload["effective_fragments"]), inspector_status=payload.get("inspector_status") or "PENDING",
        pages=dict(pages))
    payload["parts"] = part_decisions(db, group)
    card["explanation"] = explanation_block(
        delta=delta, param=group.param, expected=card.get("expected_value"), actual=card.get("actual_value"),
        confidence=group.confidence, rule_version=card["rule"].get("rule_version"))
    payload["hypothesis"] = hypothesis_card(group, delta, {"expected": card.get("source_expected"), "actual": card.get("source_actual")},
                                            payload.get("inspector_status") or "PENDING",
                                            history=hypothesis_history(db, group) if is_hypothesis(group) else None)
    if payload["hypothesis"]:  # a hypothesis has its own review status, independent of the finding decision log
        payload["inspector_status"] = card["inspector_status"] = payload["hypothesis"]["review_status"]
    payload["identity"] = {
        "object_id": group.object_id, "element": card.get("location"), "parameter": card.get("parameter_name"),
        "floor": delta.get("floor"), "section": delta.get("building_section") or delta.get("section_key"),
        "note": "Документы сопоставлены по объекту и элементу (помещению) из записи; сравнение по совпадению числа или марки не используется.",
    }
    return payload


def workbench_summary_full(db: Session, process: InspectionProcess) -> dict[str, Any]:
    summary = wb.workbench_summary(db, process)
    ids = [item["id"] for item in summary["items"]]
    part_counts: dict[int, set[str]] = defaultdict(set)
    if ids:
        for gid, key in (db.query(InspectorEdit.evidence_group_id, InspectorEdit.entity_key)
                         .filter(InspectorEdit.evidence_group_id.in_(ids), InspectorEdit.entity_type == ENTITY_PART).all()):
            part_counts[int(gid)].add(key)
    groups = {int(g.id): g for g in db.query(EvidenceGroup).filter(EvidenceGroup.id.in_(ids)).all()} if ids else {}
    for item in summary["items"]:
        group = groups.get(item["id"])
        delta = group.delta if group is not None and isinstance(group.delta, dict) else {}
        parts = compound_parts(delta)
        item["compound_parts"] = len(parts)
        item["parts_decided"] = len(part_counts.get(item["id"], ()))
        state = no_result_state(
            finding_status=item["finding_status"], comparability_status=item.get("comparability_status"), delta=delta,
            visible_stages=item["stages"], low_quality=bool(item.get("low_quality")), has_fragments=bool(item["fragments"]),
            inspector_status=item["inspector_status"])
        item["no_result"] = state["state"] if state else None
        item["description"] = (delta.get("description") or delta.get("reason") or "") if delta else ""
        item["hypothesis"] = bool(item.get("is_suspicion"))
        if item["hypothesis"] and group is not None:
            item["inspector_status"] = hypothesis_review_status(group)
    summary["no_result_titles"] = STATE_TITLES
    return summary


# ---------------------------------------------------------------------------------------------------------------
# progress panel
# ---------------------------------------------------------------------------------------------------------------
_SKIP_LABELS = {
    "documents_partial": "прочитаны только первые страницы",
    "documents_no_text_layer": "нет текстового слоя (скан)",
    "documents_failed": "ошибка чтения",
    "documents_not_scanned": "не просмотрено из-за лимита",
    "documents_not_scannable": "не PDF — не просматривается",
    "documents_not_applicable": "не применимо: параметрам матрицы эта стадия или раздел не нужны",
}


def progress_panel(db: Session, process: InspectionProcess) -> dict[str, Any]:
    documents = current_document_versions(_project_documents(db, process))
    coverage = live_tagger_coverage(documents)
    by_stage: Counter = Counter()
    mixed = 0
    for document in documents:  # the same stage rule as the protocol's upload section (annex 2), not the tagger's stricter one
        raw = str(document.dataset_stage or "").upper()
        stage_of = raw if raw in wb.STAGES else _STAGE_FROM_DOC.get(str(document.doc_stage or ""))
        if stage_of:
            by_stage[stage_of] += 1
        elif raw == "RD_ID_MIXED":
            mixed += 1
    stages = []
    incomplete_reasons: list[str] = []
    for stage in wb.STAGES:
        row = (coverage.get("stages") or {}).get(stage) or {}
        total = int(by_stage.get(stage) or 0)
        skips = [{"reason": key, "label": label, "count": int(row.get(key) or 0)} for key, label in _SKIP_LABELS.items() if row.get(key)]
        unconfirmed = mixed if stage in ("RD", "ID") and not total else 0
        stages.append({"stage": stage, "documents_total": total, "documents_scanned": int(row.get("documents_scanned") or 0),
                       "documents_scannable": int(row.get("documents_scannable") or 0), "pages_scanned": int(row.get("pages_scanned") or 0),
                       "pages_total": int(row.get("pages_total") or 0), "skips": skips, "uploaded": total > 0,
                       "stage_unconfirmed_documents": unconfirmed})
        if not total and unconfirmed:
            incomplete_reasons.append(f"{wb_stage_title(stage)}: {unconfirmed} файл(ов) со смешанной стадией РД/ИД — стадия не подтверждена по реестру, живой поиск их не просматривал")
        elif not total:
            incomplete_reasons.append(f"{wb_stage_title(stage)}: документы не загружены")
        for skip in skips:
            if skip["reason"] in ("documents_partial", "documents_failed", "documents_not_scanned", "documents_no_text_layer"):
                incomplete_reasons.append(f"{wb_stage_title(stage)}: {skip['label']} — {skip['count']}")
        if row.get("pages_total") and row.get("pages_scanned", 0) < row.get("pages_total", 0):
            incomplete_reasons.append(f"{wb_stage_title(stage)}: просмотрено {row['pages_scanned']} из {row['pages_total']} страниц")
    if not coverage.get("enabled"):
        incomplete_reasons.append("сканирование листов выключено: найдены только кандидаты правил с известным расположением")
    groups = db.query(EvidenceGroup).filter(EvidenceGroup.process_id == str(process.id)).all()
    found = [g for g in groups if g.finding_status in ("CANDIDATE", "CONFIRMED_VIOLATION") or g.decisions]
    reviewed = [g for g in found if g.decisions]
    blocked = Counter()
    for g in groups:
        if g.finding_status in ("MISSING_EVIDENCE", "NOT_COMPARABLE", "CLARIFICATION_REQUIRED"):
            delta = g.delta if isinstance(g.delta, dict) else {}
            blocked[_reason_of(delta) or "—"] += 1
    skipped = [{"reason": reason, "label": _REASON_TEXT.get(reason, "причина не указана"), "count": count} for reason, count in blocked.most_common()]
    return {
        "process_id": str(process.id), "status": process.status, "stages": stages, "coverage_summary": coverage.get("summary"),
        "mixed_stage_documents": mixed,
        "candidates_found": len(found), "candidates_reviewed": len(reviewed),
        "parameters_total": len(groups), "parameters_without_result": sum(blocked.values()), "skipped_parameters": skipped,
        "incomplete": bool(incomplete_reasons), "incomplete_reasons": incomplete_reasons,
        "note": "Частичный результат: перечисленные листы и документы не просматривались; отсутствие кандидата по ним не означает соответствия."
        if incomplete_reasons else None,
    }


# ---------------------------------------------------------------------------------------------------------------
# protocol entry
# ---------------------------------------------------------------------------------------------------------------
def _protocol_section(group: EvidenceGroup, inspector_status: str) -> dict[str, str]:
    delta = group.delta if isinstance(group.delta, dict) else {}
    status = str(group.finding_status)
    if status == "SUSPICION" or delta.get("matrix_scope") == "FREE_SEARCH":
        return {"key": "section_6_ai_suspicions", "title": "Раздел 6. Подозрения ИИ"}
    if status == "CONFIRMED_VIOLATION":
        return ({"key": "section_4_critical_violations", "title": "Раздел 4. Критические нарушения"} if group.review_priority == "HIGH"
                else {"key": "section_5_substantial_violations", "title": "Раздел 5. Существенные нарушения"})
    if status in ("MISSING_EVIDENCE", "NOT_COMPARABLE", "CLARIFICATION_REQUIRED"):
        return {"key": "section_3_unverified_parameters", "title": "Раздел 3. Параметры без результата сравнения"}
    if status == "CANDIDATE":
        return {"key": "pending", "title": "Кандидат: попадёт в протокол после решения инспектора"}
    return {"key": "checked", "title": "Проверено, нарушение не подтверждено"}


def protocol_entry(db: Session, group: EvidenceGroup) -> dict[str, Any]:
    full = workbench_group_full(db, group)
    base = evidence_group_to_dict(db, group, include_fragments=True, include_inspector_evidence=False)
    check = evidence_group_to_submission_check(base)
    protocol = latest_protocol(db, project_id=int(group.project_id), organization_id=int(group.organization_id),
                               process_id=str(group.process_id), include_payload=False)
    decisions = base.get("decisions") or []
    last = decisions[-1] if decisions else None
    return {
        "protocol": {"id": int(protocol.id), "version": int(protocol.version), "status": protocol.status} if protocol else None,
        "section": _protocol_section(group, full.get("inspector_status") or "PENDING"),
        "finding_id": check.get("finding_id"), "evidence_group_id": check.get("evidence_group_id"),
        "check": check,
        "status": {"finding_status": group.finding_status, "inspector_status": full.get("inspector_status"),
                   "protocol_status": check.get("protocol_status")},
        "decision": last, "decision_rationale": (last or {}).get("comment"), "decisions": decisions,
        "parts": full["parts"], "explanation": full["card"]["explanation"], "no_result": full["no_result"], "hypothesis": full["hypothesis"],
        "sources": {"expected": full["card"].get("source_expected"), "actual": full["card"].get("source_actual"),
                    "fragments": [{"key": i["key"], "stage": i["stage"], "origin": i["origin"], "status": i["status"], "version": i["version"],
                                   "sheet": (i.get("sheet") or {}).get("label"), "stage_info": i.get("stage_info"), **{k: i["current"].get(k) for k in (
                                       "file", "file_id", "document_code", "revision", "page", "bbox_pdf", "extracted_value", "role")}}
                                  for i in full["effective_fragments"]]},
        "edits": full["edits"],
        "note": "Запись протокола: источники, значения, статус, решение и обоснование одной находки.",
    }
