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
  DRAFT: 'Черновик',
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
