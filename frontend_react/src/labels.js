export const FINDING_STATUS_LABELS = {
  CANDIDATE: 'Кандидат на нарушение',
  CONFIRMED_VIOLATION: 'Нарушение подтверждено',
  NEGATIVE_VERIFIED: 'Несоответствие не подтверждено',
  MISSING_EVIDENCE: 'Недостаточно доказательств',
  NOT_APPLICABLE: 'Неприменимо',
  NOT_COMPARABLE: 'Сравнение невозможно',
  CLARIFICATION_REQUIRED: 'Требуется уточнение',
  SUSPICION: 'Подозрение ИИ',
};

export const FINDING_TONE = {
  CANDIDATE: 'critical',
  CONFIRMED_VIOLATION: 'critical',
  NEGATIVE_VERIFIED: 'ok',
  MISSING_EVIDENCE: 'warn',
  NOT_APPLICABLE: 'neutral',
  NOT_COMPARABLE: 'warn',
  CLARIFICATION_REQUIRED: 'warn',
  SUSPICION: 'review',
};

export const PROCESS_STATUS_LABELS = {
  PENDING: 'Ожидает запуска',
  QUEUED: 'В очереди',
  PROCESSING: 'Обрабатывается',
  PARSING: 'Разбор документов',
  READY: 'Готово к проверке',
  FAILED: 'Ошибка обработки',
  VERIFYING: 'Инспектор проверяет',
  COMPLETED: 'Проверено инспектором',
  FINALIZED: 'Протокол финализирован',
};

export const PROCESS_TONE = {
  PENDING: 'neutral',
  QUEUED: 'warn',
  PROCESSING: 'warn',
  PARSING: 'warn',
  READY: 'ok',
  FAILED: 'critical',
  VERIFYING: 'warn',
  COMPLETED: 'ok',
  FINALIZED: 'neutral',
};

export const INSPECTOR_STATUS_LABELS = {
  PENDING: 'Ожидает решения',
  CONFIRMED: 'Подтверждено инспектором',
  REJECTED: 'Отклонено инспектором',
  CLARIFICATION_REQUIRED: 'Требуется уточнение',
};

export const DATASET_STAGE_LABELS = {
  PD: 'ПД',
  RD: 'РД',
  ID: 'ИД',
  UNKNOWN: 'Не указано',
};

export const STAGE_LABELS = {
  project: 'ПД',
  working: 'РД',
  as_built: 'ИД',
  ifc: 'IFC',
  unknown: 'Не указано',
};

export const FRAGMENT_ROLE_LABELS = {
  expected: 'Ожидаемое значение (ПД)',
  actual: 'Фактическое значение (РД/ИД)',
  context: 'Контекст',
};

export const APPROVAL_STATUS_LABELS = {
  APPROVED: 'Утверждён',
  FOR_CONSTRUCTION: 'В производство работ',
  DRAFT: 'Черновик',
  SUPERSEDED: 'Заменён',
  CANCELLED: 'Аннулирован',
  UNKNOWN: 'Статус не указан',
};

export const REVIEW_PRIORITY_LABELS = {
  HIGH: 'Высокий приоритет',
  MEDIUM: 'Средний приоритет',
  LOW: 'Низкий приоритет',
};

export const REJECT_REASON_CODES = [
  { value: 'NOT_A_VIOLATION', label: 'Несоответствие не подтверждено фактически' },
  { value: 'STALE_REVISION', label: 'Сравнение с неактуальной редакцией документа' },
  { value: 'WRONG_LOCALIZATION', label: 'Доказательство привязано не к тому месту/листу' },
  { value: 'DATA_ERROR', label: 'Ошибка распознавания исходных данных' },
  { value: 'OTHER', label: 'Другая причина (указать в комментарии)' },
];

export function label(map, value, fallback) {
  if (value === null || value === undefined) return fallback ?? '—';
  return map[value] || value || fallback || '—';
}

export const STAGE_TITLES = { PD: 'ПД', RD: 'РД', ID: 'ИД' };
export const STAGE_FULL = { PD: 'Проектная документация', RD: 'Рабочая документация', ID: 'Исполнительная документация' };

export const EDIT_ACTION_LABELS = {
  ADD: 'Добавлен',
  REFINE: 'Уточнён',
  REMOVE: 'Исключён',
  RESTORE: 'Восстановлен',
  CHOOSE: 'Выбрана редакция',
};

export const FRAGMENT_STATUS_LABELS = { ACTIVE: 'в доказательстве', REMOVED: 'исключён', ORPHANED: 'машинный фрагмент пересчитан' };

export const ROLE_LABELS = { expected: 'эталон (ПД)', actual: 'фактическое', context: 'контекст' };

export const REVISION_SCOPE_TYPES = {
  MIXED_PROJECT_BASELINES: 'Разные серии шифров в одной стадии',
  UNORDERED_REVISIONS: 'Редакции не упорядочены',
  SUPERSEDED_BY_NEWER_REVISION: 'Есть более новая редакция',
  PREDECESSOR_CHAIN: 'Цепочка редакций (predecessor/successor)',
};

export const REVISION_STATUS_LABELS = {
  CLARIFICATION_REQUIRED: 'Требуется уточнение',
  RESOLVED: 'Определена системой',
  RESOLVED_BY_INSPECTOR: 'Выбрана инспектором',
};

export const COMPLETENESS_STATUS_LABELS = {
  COMPLETE: 'Комплект полный',
  INCOMPLETE: 'Комплект неполный',
  CLARIFICATION_REQUIRED: 'Требуется уточнение',
  UPLOADED: 'Загружено',
  MISSING_EVIDENCE: 'Нет документа',
  NOT_APPLICABLE: 'Неприменимо',
  NOT_COMPARABLE: 'Нечитаемо',
  UNCERTAIN: 'Неопределённость',
};

export const COMPLETENESS_TONE = {
  COMPLETE: 'ok', UPLOADED: 'ok', INCOMPLETE: 'critical', MISSING_EVIDENCE: 'critical',
  CLARIFICATION_REQUIRED: 'warn', UNCERTAIN: 'warn', NOT_APPLICABLE: 'neutral', NOT_COMPARABLE: 'warn',
};

export const DECISION_OPTIONS = [
  { key: 'Confirm', hotkey: 'C', label: 'Подтвердить', status: 'CONFIRMED_VIOLATION', tone: 'critical' },
  { key: 'Reject', hotkey: 'R', label: 'Отклонить', status: 'NEGATIVE_VERIFIED', tone: 'ok' },
  { key: 'Clarification Required', hotkey: 'U', label: 'Уточнить', status: 'CLARIFICATION_REQUIRED', tone: 'warn' },
];

export const DECIDABLE_STATUSES = new Set(['CANDIDATE', 'CONFIRMED_VIOLATION', 'NEGATIVE_VERIFIED', 'CLARIFICATION_REQUIRED']);
