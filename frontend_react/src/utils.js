export function fmtDate(value) {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString('ru-RU');
}

export function formatValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

export function formatBBox(value) {
  if (!Array.isArray(value) || !value.length) return null;
  return value
    .map((v) => {
      const num = Number(v);
      return Number.isFinite(num) ? num.toFixed(Math.abs(num) <= 1 ? 3 : 1) : String(v);
    })
    .join(', ');
}

export function formatDelta(delta) {
  if (!delta) return null;
  if (delta.reason) return null;
  if (delta.equal === true) return 'Совпадает с ожидаемым значением';
  if (delta.delta !== undefined && delta.delta !== null) {
    const num = Number(delta.delta);
    const sign = num > 0 ? '+' : '';
    return `Отклонение: ${sign}${num}`;
  }
  return null;
}

const MISSING_REASON_LABELS = {
  no_relevant_evidence: 'релевантные доказательства не найдены',
  values_not_extracted: 'значения не извлечены',
  mixed_stage_requires_review: 'смешанная стадия РД/ИД требует уточнения',
  missing_stage: 'отсутствует требуемая стадия документа',
};

export function buildExplanation(group) {
  const param = group.parameter || {};
  const delta = group.delta || {};
  const parts = [];
  const name = param.name ? `«${param.name}»` : 'параметр';

  if (delta.reason) {
    const reasonText = MISSING_REASON_LABELS[delta.reason] || delta.reason;
    parts.push(`Сравнить ${name} не удалось: ${reasonText}.`);
  } else if (group.finding_status === 'CONFIRMED_VIOLATION' || group.finding_status === 'CANDIDATE') {
    parts.push(
      `Ожидаемое значение ${name} по проектной документации — «${formatValue(group.expected)}»,` +
        ` фактическое по РД/ИД — «${formatValue(group.actual)}». Значения не совпадают.`,
    );
  } else if (group.finding_status === 'NEGATIVE_VERIFIED') {
    parts.push(`Значение ${name} совпадает: «${formatValue(group.expected)}» = «${formatValue(group.actual)}». Нарушение не подтверждено.`);
  } else if (group.finding_status === 'SUSPICION') {
    parts.push(`ИИ обнаружил потенциальное несоответствие по ${name} вне штатной матрицы параметров — требуется проверка инспектором.`);
  } else if (group.finding_status === 'NOT_APPLICABLE') {
    parts.push(`${name[0].toUpperCase()}${name.slice(1)} неприменим к данному объекту/сущности.`);
  }

  if (group.comparison_scenario) {
    parts.push(`Сценарий сопоставления: ${group.comparison_scenario}.`);
  }
  const context = (group.fragments || []).find((f) => f.context)?.context;
  if (context) {
    const clipped = context.length > 220 ? `${context.slice(0, 220).trim()}…` : context;
    parts.push(`Контекст источника: «${clipped}»`);
  }
  return parts.join(' ');
}
