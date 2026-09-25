import { useState } from 'react';
import { FINDING_STATUS_LABELS, FINDING_TONE, INSPECTOR_STATUS_LABELS, REJECT_REASON_CODES, STAGE_TITLES, label } from '../labels.js';
import { fmtConfidence, formatValue } from '../utils.js';

export const QUEUE_MODES = [
  { key: 'candidates', label: 'Кандидаты', test: (i) => i.finding_status === 'CANDIDATE' },
  { key: 'lowconf', label: 'Низкая уверенность / LOW_QUALITY', test: (i, t) => i.low_quality || (i.min_confidence !== null && i.min_confidence !== undefined && i.min_confidence < t) },
  { key: 'clarify', label: 'Уточнение', test: (i) => i.finding_status === 'CLARIFICATION_REQUIRED' },
  { key: 'decided', label: 'Решённые', test: (i) => i.inspector_status !== 'PENDING' },
  { key: 'suspicion', label: 'Подозрения ИИ', test: (i) => i.is_suspicion },
  { key: 'all', label: 'Все', test: () => true },
];

const PRIORITY_RANK = { HIGH: 0, MEDIUM: 1, LOW: 2 };

export function filterQueue(items, { mode, status, section, search, threshold }) {
  const modeDef = QUEUE_MODES.find((m) => m.key === mode) || QUEUE_MODES[0];
  const needle = search.trim().toLowerCase();
  return items
    .filter((i) => modeDef.test(i, threshold))
    .filter((i) => status === 'ALL' || i.finding_status === status)
    .filter((i) => section === 'ALL' || i.section === section)
    .filter((i) => !needle || [i.matrix_code, i.legacy_code, i.name, i.location, i.expected, i.actual].some((v) => String(v || '').toLowerCase().includes(needle)))
    .sort((a, b) =>
      (a.finding_status === 'CANDIDATE' ? 0 : 1) - (b.finding_status === 'CANDIDATE' ? 0 : 1)
      || (PRIORITY_RANK[a.review_priority] ?? 3) - (PRIORITY_RANK[b.review_priority] ?? 3)
      || String(a.type_key).localeCompare(String(b.type_key))
      || String(a.location || '').localeCompare(String(b.location || ''), 'ru', { numeric: true })
      || a.id - b.id);
}

export default function CandidateQueue({ summary, rows, filters, onFilters, selectedId, onSelect, checked, onChecked, onBulk, locked }) {
  const items = summary?.items || [];
  const counts = Object.fromEntries(QUEUE_MODES.map((m) => [m.key, items.filter((i) => m.test(i, filters.threshold)).length]));
  const checkedRows = items.filter((i) => checked.has(i.id));
  const types = new Set(checkedRows.map((i) => i.type_key));
  const sameType = types.size === 1;
  const decidable = checkedRows.every((i) => ['CANDIDATE', 'CONFIRMED_VIOLATION', 'NEGATIVE_VERIFIED', 'CLARIFICATION_REQUIRED'].includes(i.finding_status));

  function toggle(id) {
    const next = new Set(checked);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChecked(next);
  }

  function checkSameType() {
    const current = items.find((i) => i.id === selectedId);
    if (!current) return;
    onChecked(new Set(rows.filter((i) => i.type_key === current.type_key && i.finding_status === current.finding_status).map((i) => i.id)));
  }

  return (
    <aside className="queue">
      <div className="queue-modes">
        {QUEUE_MODES.map((m) => (
          <button key={m.key} type="button" className={`queue-mode${filters.mode === m.key ? ' active' : ''}`} onClick={() => onFilters({ mode: m.key })}>
            {m.label} <span className="count">{counts[m.key]}</span>
          </button>
        ))}
      </div>

      <div className="queue-filters">
        <input className="input" placeholder="Поиск: код, помещение, значение" value={filters.search} onChange={(e) => onFilters({ search: e.target.value })} />
        <div className="row" style={{ gap: 6 }}>
          <select className="select" value={filters.section} onChange={(e) => onFilters({ section: e.target.value })} title="Раздел">
            <option value="ALL">Все разделы</option>
            {(summary?.sections || []).map((s) => <option key={s.section} value={s.section}>{s.section} ({s.count})</option>)}
          </select>
          <select className="select" value={filters.status} onChange={(e) => onFilters({ status: e.target.value })} title="Статус">
            <option value="ALL">Все статусы</option>
            {Object.keys(FINDING_STATUS_LABELS).map((s) => <option key={s} value={s}>{FINDING_STATUS_LABELS[s]}</option>)}
          </select>
        </div>
        {filters.mode === 'lowconf' && (
          <label className="row small muted" style={{ gap: 6 }}>
            Порог confidence
            <input className="input thin" type="number" step="0.05" min="0" max="1" value={filters.threshold} onChange={(e) => onFilters({ threshold: Number(e.target.value) })} />
            <span>(по умолчанию {summary?.low_confidence_threshold})</span>
          </label>
        )}
      </div>

      {checked.size > 0 && (
        <div className={`bulk-bar${sameType ? '' : ' invalid'}`}>
          <div className="small">
            Выбрано <b>{checked.size}</b>{sameType ? <> • тип <b className="mono">{[...types][0]}</b></> : null}
          </div>
          {!sameType && <div className="small">Групповые операции — только для однотипных кандидатов (один код параметра).</div>}
          {sameType && !decidable && <div className="small">Среди выбранных есть технические статусы — решение для них не принимается.</div>}
          <div className="row wrap" style={{ gap: 4 }}>
            <button className="btn tiny" type="button" disabled={!sameType || !decidable || locked} onClick={() => onBulk('Confirm')}>Подтвердить все</button>
            <button className="btn tiny" type="button" disabled={!sameType || !decidable || locked} onClick={() => onBulk('Clarification Required')}>Уточнить все</button>
            <button className="btn tiny danger" type="button" disabled={!sameType || !decidable || locked} onClick={() => onBulk('Reject')}>Отклонить все…</button>
            <button className="btn tiny" type="button" onClick={() => onChecked(new Set())}>Снять</button>
          </div>
        </div>
      )}

      <div className="queue-list" role="listbox" aria-label="Очередь кандидатов">
        {rows.length === 0 && <div className="empty-state">В этой очереди пусто.</div>}
        {rows.map((i) => (
          <div
            key={i.id}
            role="option"
            aria-selected={i.id === selectedId}
            className={`queue-row${i.id === selectedId ? ' selected' : ''}${i.inspector_status !== 'PENDING' ? ' decided' : ''}`}
            onClick={() => onSelect(i.id)}
          >
            <input
              type="checkbox"
              checked={checked.has(i.id)}
              onClick={(e) => e.stopPropagation()}
              onChange={() => toggle(i.id)}
              aria-label={`Выбрать ${i.matrix_code || i.legacy_code} ${i.location || ''}`}
            />
            <div className="queue-main">
              <div className="row" style={{ gap: 6 }}>
                <span className="mono code">{i.matrix_code || i.legacy_code}</span>
                <span className="loc">{formatValue(i.location)}</span>
                <span className={`status-dot tone-${FINDING_TONE[i.finding_status] || 'neutral'}`} title={label(FINDING_STATUS_LABELS, i.finding_status)} />
              </div>
              <div className="queue-name">{i.name}</div>
              <div className="queue-meta">
                {(i.stages || []).map((s) => <span key={s} className={`stage-chip mini stage-${s}`}>{STAGE_TITLES[s]}</span>)}
                <span className="muted">conf {fmtConfidence(i.min_confidence ?? i.confidence)}</span>
                {i.low_quality && <span className="flag lq" title={(i.low_quality_reasons || []).join('; ')}>LQ</span>}
                {i.min_confidence !== null && i.min_confidence !== undefined && i.min_confidence < filters.threshold && <span className="flag lc">низк.</span>}
                {i.edits > 0 && <span className="flag ed" title="Доказательства уточнены инспектором">✎{i.edits}</span>}
                {i.inspector_status !== 'PENDING' && <span className="flag ok">{label(INSPECTOR_STATUS_LABELS, i.inspector_status)}</span>}
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="queue-foot muted small">
        <button className="btn tiny" type="button" onClick={checkSameType} disabled={!selectedId}>Отметить однотипные</button>
        <span><kbd>J</kbd>/<kbd>K</kbd> — следующий/предыдущий</span>
      </div>
    </aside>
  );
}

export function BulkDialog({ decision, rows, onCancel, onSubmit }) {
  const [comment, setComment] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [reasonCode, setReasonCode] = useState(REJECT_REASON_CODES[0].value);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const isReject = decision === 'Reject';
  const title = { Confirm: 'Подтвердить нарушения', Reject: 'Отклонить кандидатов', 'Clarification Required': 'Запросить уточнение' }[decision];
  const canSubmit = comment.trim().length >= 3 && (!isReject || confirmed) && !busy;

  async function submit() {
    setBusy(true);
    setError('');
    try {
      await onSubmit({ comment: comment.trim(), confirm: confirmed, reasonCode });
    } catch (err) {
      setError(err.message || 'Не удалось выполнить групповую операцию');
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={() => !busy && onCancel()}>
      <div className="modal-box wide" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={title}>
        <h3>{title}: {rows.length} шт. ({rows[0]?.type_key})</h3>
        <div className="bulk-list">
          {rows.map((r) => <div key={r.id} className="small"><span className="mono">{r.matrix_code}</span> {formatValue(r.location)} — {formatValue(r.expected)} → {formatValue(r.actual)}</div>)}
        </div>
        {isReject && (
          <label className="field">
            <span>Общая причина (reason_code)</span>
            <select className="select" value={reasonCode} onChange={(e) => setReasonCode(e.target.value)}>
              {REJECT_REASON_CODES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </label>
        )}
        <label className="field">
          <span>Общий комментарий (обязателен, попадёт в каждое решение)</span>
          <textarea className="textarea" autoFocus value={comment} onChange={(e) => setComment(e.target.value)} />
        </label>
        {isReject && (
          <label className="confirm-check">
            <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            <span>Я проверил каждого кандидата и подтверждаю <b>массовое отклонение {rows.length}</b> однотипных кандидатов. Решение попадёт в журнал как групповое.</span>
          </label>
        )}
        {error && <div className="error-banner"><span>{error}</span></div>}
        <div className="modal-actions">
          <button className="btn" type="button" onClick={onCancel} disabled={busy}>Отмена</button>
          <button className={`btn primary${isReject ? ' danger-fill' : ''}`} type="button" onClick={submit} disabled={!canSubmit}>
            {busy ? 'Сохранение…' : isReject ? `Отклонить ${rows.length}` : `Применить к ${rows.length}`}
          </button>
        </div>
      </div>
    </div>
  );
}
