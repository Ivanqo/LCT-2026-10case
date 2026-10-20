import { useEffect, useState } from 'react';
import { api, downloadProtectedFile, protocolPdfUrl } from '../api.js';
import Badge from './Badge.jsx';
import { FINDING_STATUS_LABELS, INSPECTOR_STATUS_LABELS, NO_RESULT_TONE, HYPOTHESIS_METHOD_LABELS, PROTOCOL_STATUS_LABELS, STAGE_TITLES, cleanRevision, label, rejectReasonText } from '../labels.js';

const HYPOTHESIS_ACTIONS = [
  { status: 'CONFIRMED', label: 'Принять гипотезу' },
  { status: 'REJECTED', label: 'Отклонить гипотезу' },
  { status: 'CLARIFICATION_REQUIRED', label: 'Запросить уточнение' },
  { status: 'TO_CANDIDATE', label: 'Преобразовать в кандидата' },
];
import { fmtConfidence, fmtDate, formatValue } from '../utils.js';

const NOT_COMPUTED = <span className="muted">не вычислялось</span>;

function computed(value, suffix = '') {
  if (value === null || value === undefined || value === '') return NOT_COMPUTED;
  return `${value}${suffix}`;
}

/** A state without a result (checklist 7): the reason and the action that is available now. */
export function NoResultNotice({ state, locked, onAction }) {
  if (!state) return null;
  const action = state.action || {};
  const actionable = ['upload', 'revisions', 'edit_evidence'].includes(action.kind);
  return (
    <div className={`no-result tone-${NO_RESULT_TONE[state.state] || 'warn'}`} role="status">
      <div className="row wrap" style={{ gap: 8 }}>
        <Badge tone={NO_RESULT_TONE[state.state] || 'warn'}>{state.title}</Badge>
        <span className="small">{state.reason}</span>
      </div>
      <div className="row wrap small" style={{ gap: 8, marginTop: 6 }}>
        <span className="muted">Что можно сделать:</span>
        {actionable ? (
          <button className="btn tiny primary" type="button" disabled={locked && action.kind === 'upload'} onClick={() => onAction(action)}>
            {action.label}
          </button>
        ) : (
          <span>{action.label}</span>
        )}
      </div>
    </div>
  );
}

/** The "Основание" block (checklist 6): only what the pipeline actually computed; the rest reads "не вычислялось". */
export function ExplanationBlock({ card }) {
  const e = card.explanation;
  if (!e) return null;
  const unit = e.unit ? ` ${e.unit}` : '';
  const stages = ['PD', 'RD', 'ID'].filter((s) => card.values_by_stage?.[s] !== null && card.values_by_stage?.[s] !== undefined);
  return (
    <details className="explain-block" open>
      <summary>Основание расхождения</summary>
      <dl className="card-grid">
        <dt>Уровень выдачи</dt><dd>{e.evidence_tier === 'SUGGESTION' ? 'Предположение для верификации инспектором' : e.evidence_tier === 'VIOLATION_CANDIDATE' ? 'Нарушение-кандидат' : NOT_COMPUTED}{Array.isArray(e.tier_reasons) && e.tier_reasons.length ? <span className="muted small"> • {e.tier_reasons.join('; ')}</span> : null}</dd>
        <dt>Параметр</dt><dd>{formatValue(card.parameter_name)}{card.matrix_code ? <span className="muted mono"> • {card.matrix_code}</span> : null}</dd>
        <dt>Объект сравнения</dt><dd>{formatValue(card.location)} <span className="muted mono">({formatValue(card.object_id)})</span></dd>
        <dt>Значения по стадиям</dt>
        <dd>
          {stages.length ? stages.map((s) => (
            <span key={s} style={{ marginRight: 12 }}>
              <span className={`stage-chip mini stage-${s}`}>{STAGE_TITLES[s]}</span> <b>{formatValue(card.values_by_stage[s])}</b>{card.unit && /^[\s\d.,]+$/.test(String(formatValue(card.values_by_stage[s]))) ? <span className="muted"> {card.unit}</span> : null}
            </span>
          )) : NOT_COMPUTED}
        </dd>
        <dt>Отклонение</dt>
        <dd>
          {e.deviation_abs !== null && e.deviation_abs !== undefined
            ? <><b>{e.deviation_abs}{unit}</b>{e.deviation_pct !== null && e.deviation_pct !== undefined ? <> ({e.deviation_pct} %)</> : null}<span className="muted small"> • {e.deviation_basis}</span></>
            : NOT_COMPUTED}
        </dd>
        <dt>Правило оценки</dt><dd>{computed(e.rule)}{e.rule_evaluation ? <span className="muted small"> • {e.rule_evaluation}</span> : null}{e.needs_review ? <span className="pill warn-pill" style={{ marginLeft: 6 }}>порог требует проверки</span> : null}</dd>
        <dt>Условие</dt><dd>{computed(e.condition)}</dd>
        <dt>Порог</dt><dd>{computed(e.threshold_text)}</dd>
        <dt>Основание порога</dt><dd>{computed(e.threshold_basis)}</dd>
      </dl>
      <div className="confidence-split" aria-label="Уверенности и сходство показываются раздельно">
        <div className="value-box"><div className="l">Уверенность распознавания текста</div><div className="v">{e.text_confidence !== null && e.text_confidence !== undefined ? fmtConfidence(e.text_confidence) : NOT_COMPUTED}</div></div>
        <div className="value-box"><div className="l">Уверенность привязки</div><div className="v">{e.link_confidence !== null && e.link_confidence !== undefined ? fmtConfidence(e.link_confidence) : NOT_COMPUTED}</div></div>
        <div className="value-box"><div className="l">Сходство листов</div><div className="v">{e.similarity !== null && e.similarity !== undefined ? fmtConfidence(e.similarity) : NOT_COMPUTED}</div></div>
        <div className="value-box"><div className="l">Общая (минимум по цепочке)</div><div className="v">{fmtConfidence(e.combined_confidence)}</div></div>
      </div>
      <div className="muted small" style={{ marginTop: 6 }}>{e.probability_note}</div>
    </details>
  );
}

function refText(ref) {
  if (!ref) return NOT_COMPUTED;
  if (typeof ref === 'string') return ref;
  const file = ref.file || ref.file_id;
  const parts = [file, ref.page ? `стр. ${ref.page}` : null, ref.document_code ? `шифр ${ref.document_code}` : null, ref.revision ? `ред. ${cleanRevision(ref.revision)}` : null].filter(Boolean);
  return parts.length ? parts.join(' • ') : JSON.stringify(ref);
}

/** Free-search hypothesis (ТЗ 9.5): method, confidence, description, links to ПД/РД, criticality, basis, review status. */
export function HypothesisCard({ hypothesis, groupId, locked, onReviewed }) {
  const [status, setStatus] = useState(null);
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setStatus(null);
    setComment('');
    setError('');
  }, [groupId]);

  if (!hypothesis) return null;
  const history = hypothesis.review_history || [];

  async function save() {
    setBusy(true);
    setError('');
    try {
      const result = status === 'TO_CANDIDATE'
        ? await api.hypothesisToCandidate(groupId, { comment: comment.trim() })
        : await api.hypothesisReview(groupId, { status, comment: comment.trim() });
      setStatus(null);
      setComment('');
      onReviewed(result);
    } catch (err) {
      setError(err.message || 'Не удалось сохранить статус рассмотрения');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="hypothesis-card" aria-label="Карточка гипотезы">
      <div className="row wrap" style={{ gap: 8 }}>
        <Badge tone="review">Гипотеза свободного поиска</Badge>
        <span className="muted small">{hypothesis.note}</span>
      </div>
      <dl className="card-grid">
        <dt>Метод обнаружения</dt><dd title={hypothesis.method ? `Код метода: ${hypothesis.method}` : undefined}>{hypothesis.method ? label(HYPOTHESIS_METHOD_LABELS, hypothesis.method) : NOT_COMPUTED}{hypothesis.method_detail ? <span className="muted"> • {hypothesis.method_detail}</span> : null}</dd>
        <dt>Уверенность</dt><dd>{fmtConfidence(hypothesis.confidence)}</dd>
        <dt>Описание</dt><dd>{computed(hypothesis.description)}</dd>
        <dt>Ссылка на ПД</dt><dd>{refText(hypothesis.pd_reference)}</dd>
        <dt>Ссылка на РД</dt><dd>{refText(hypothesis.rd_reference)}</dd>
        <dt>Критичность</dt><dd>{computed(hypothesis.criticality)}</dd>
        <dt>Нормативное основание</dt><dd>{computed(hypothesis.normative_basis)}</dd>
        <dt>Статус рассмотрения</dt><dd>{label(INSPECTOR_STATUS_LABELS, hypothesis.review_status)} <span className="muted">• {label(FINDING_STATUS_LABELS, hypothesis.finding_status)}</span></dd>
      </dl>
      <div className="hypothesis-review">
        <div className="row wrap" style={{ gap: 6 }}>
          <span className="muted small">Рассмотрение гипотезы:</span>
          {HYPOTHESIS_ACTIONS.map((a) => (
            <button key={a.status} type="button" className={`btn tiny${status === a.status ? ' toggled' : ''}`} disabled={locked || busy} onClick={() => setStatus(a.status)}>{a.label}</button>
          ))}
          {locked && <span className="muted small">Протокол финализирован — рассмотрение заблокировано.</span>}
        </div>
        {status && (
          <div className="top-gap">
            <label className="field">
              <span>Основание рассмотрения — обязательно</span>
              <textarea className="textarea" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} aria-label="Основание рассмотрения гипотезы" />
            </label>
            {error && <div className="error-banner"><span>{error}</span></div>}
            <button className="btn primary" type="button" disabled={busy || comment.trim().length < 3} onClick={save}>{busy ? 'Сохранение…' : 'Сохранить статус'}</button>
          </div>
        )}
        {history.length > 0 && (
          <ul className="small history-list">
            {[...history].reverse().map((h) => (
              <li key={h.id}>{fmtDate(h.created_at)} — <b>{label(INSPECTOR_STATUS_LABELS, h.new_value?.status)}</b>{h.reason ? `: «${h.reason}»` : ''} <span className="muted">• {h.user_login || `пользователь №${h.user_id}`}, было: {label(INSPECTOR_STATUS_LABELS, h.previous_value?.status)}</span></li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

/** Which documents the card compares, and by what: object and element from the record, never a number or mark match. */
export function IdentityStrip({ identity }) {
  if (!identity) return null;
  return (
    <div className="identity-strip small">
      <span><span className="muted">Объект:</span> <b className="mono">{formatValue(identity.object_id)}</b></span>
      <span><span className="muted">Элемент:</span> <b>{formatValue(identity.element)}</b></span>
      {identity.floor ? <span><span className="muted">Этаж:</span> <b>{identity.floor}</b></span> : null}
      {identity.section ? <span><span className="muted">Корпус/секция:</span> <b>{identity.section}</b></span> : null}
      <span className="muted">{identity.note}</span>
    </div>
  );
}

/** Jump from the card to its protocol entry and export it (checklist 11). */
export function ProtocolEntryPanel({ groupId, refreshKey }) {
  const [open, setOpen] = useState(false);
  const [entry, setEntry] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setEntry(null);
    setError('');
  }, [groupId, refreshKey]);

  useEffect(() => {
    if (!open || entry || !groupId) return undefined;
    let cancelled = false;
    setBusy(true);
    api.protocolEntry(groupId)
      .then((data) => !cancelled && setEntry(data))
      .catch((err) => !cancelled && setError(err.message || 'Не удалось загрузить запись протокола'))
      .finally(() => !cancelled && setBusy(false));
    return () => { cancelled = true; };
  }, [open, entry, groupId]);

  function exportJson() {
    const blob = new Blob([JSON.stringify(entry, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `protocol_entry_${entry.finding_id || groupId}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return (
    <section className="protocol-entry">
      <div className="row wrap" style={{ gap: 8 }}>
        <button className="btn tiny" type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          {open ? 'Скрыть запись протокола' : 'Перейти к записи протокола'}
        </button>
        {entry && <button className="btn tiny" type="button" onClick={exportJson}>Экспорт записи (JSON)</button>}
        {entry?.protocol && (
          <button className="btn tiny" type="button" onClick={() => downloadProtectedFile(protocolPdfUrl(entry.protocol.id), `protocol_${entry.protocol.id}.pdf`).catch((e) => setError(e.message))}>
            Открыть протокол (PDF)
          </button>
        )}
      </div>
      {open && busy && <div className="muted small top-gap"><span className="spinner" /> Загружаю запись…</div>}
      {open && error && <div className="error-banner top-gap"><span>{error}</span></div>}
      {open && entry && (
        <dl className="card-grid top-gap">
          <dt>Раздел протокола</dt><dd>{entry.section?.title}</dd>
          <dt>Протокол</dt><dd>{entry.protocol ? `версия ${entry.protocol.version} • ${label(PROTOCOL_STATUS_LABELS, entry.protocol.status)}` : 'версия ещё не сформирована'}</dd>
          <dt>Идентификатор находки</dt><dd className="mono">{formatValue(entry.finding_id)}</dd>
          <dt>Статус</dt><dd>{label(FINDING_STATUS_LABELS, entry.status?.finding_status)} <span className="muted">• решение инспектора: {label(INSPECTOR_STATUS_LABELS, entry.status?.inspector_status)}</span></dd>
          <dt>Решение</dt>
          <dd>{entry.decision ? <>{fmtDate(entry.decision.created_at)} — {entry.decision.decision}{entry.decision.reason_code ? ` • ${rejectReasonText(entry.decision.reason_code)}` : ''}</> : <span className="muted">решения ещё нет</span>}</dd>
          <dt>Обоснование</dt><dd>{computed(entry.decision_rationale)}</dd>
          <dt>Источники</dt><dd>{(entry.sources?.fragments || []).filter((f) => f.status === 'ACTIVE').map((f) => `${STAGE_TITLES[f.stage] || f.stage}: ${f.file_id || f.file}, стр. ${f.page}${f.sheet ? `, ${f.sheet}` : ''}`).join('; ') || '—'}</dd>
        </dl>
      )}
    </section>
  );
}
