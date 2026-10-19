"""Deterministic Module 9.4 feedback rules; these fields are rule outputs, not model predictions."""
from __future__ import annotations


REASON_SUGGESTIONS = {
    "NOT_A_VIOLATION": "Перепроверить применимость требования и условие срабатывания; инспектор не подтвердил нарушение.",
    "STALE_REVISION": "Проверить выбор актуальной редакции и цепочку редакций документов.",
    "WRONG_LOCALIZATION": "Проверить привязку доказательства к листу, помещению и элементу.",
    "DATA_ERROR": "Проверить OCR и извлечённое значение на исходном листе.",
    "OTHER": "Разобрать комментарий инспектора и уточнить причину вручную.",
}


def suggested_fix_for_reason(reason_code: str | None) -> str:
    return REASON_SUGGESTIONS.get(str(reason_code or "OTHER").strip().upper(), REASON_SUGGESTIONS["OTHER"])


def verdict_for_inspector_decision(ai_status: str | None, decision: str | None) -> tuple[str, str]:
    """Compare the pre-decision AI finding status with the inspector's decision.

    Only an explicit candidate rejected by the inspector is counted as disagreement.
    Unknown combinations are kept as UNDETERMINED rather than assigned a score.
    """
    ai = str(ai_status or "").strip().upper()
    inspector = str(decision or "").strip().upper()
    if ai == "CANDIDATE" and inspector in {"REJECT", "REJECTED"}:
        return "DISAGREE", "Правило: ИИ выдал CANDIDATE, инспектор выбрал Reject. Это расхождение статусов, не вывод модели."
    if ai in {"NEGATIVE_VERIFIED", "NOT_APPLICABLE"} and inspector in {"REJECT", "REJECTED"}:
        return "AGREE", "Правило: статус ИИ и решение инспектора указывают на отсутствие подтверждённого нарушения."
    if ai in {"CANDIDATE", "CONFIRMED_VIOLATION"} and inspector in {"CONFIRM", "CONFIRMED"}:
        return "AGREE", "Правило: статус-кандидат ИИ и подтверждение инспектора согласуются."
    if inspector in {"", "NONE", "PENDING"}:
        return "PENDING_REVIEW", "Спор открыт; итоговое решение инспектора ещё не сопоставлено."
    return "UNDETERMINED", f"Правило не сопоставило статус ИИ «{ai or 'не указан'}» и решение «{inspector or 'не указано'}»."
