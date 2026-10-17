import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { DECISION_LABEL_BY_KEY, PART_DECISION_LABELS } from '../labels.js';
import { formatValue } from '../utils.js';

/**
 * "Разделить на части" (ТЗ 9.3, checklist 8): a compound finding is decided part by part. Each part gets its own
 * decision (a reason is mandatory for a rejected or clarified part). When every part has one, the overall decision
 * is recorded by the server, so the list, the card and the protocol agree; until then the finding stays undecided.
 */
export default function PartsPanel({ detail, locked, onSaved, onClose }) {
  const parts = detail.parts?.parts || [];
  const [rows, setRows] = useState({});
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setRows({});
    setComment('');
    setError('');
  }, [detail.id]);

  const set = (key, patch) => setRows((r) => ({ ...r, [key]: { ...(r[key] || {}), ...patch } }));
  const chosen = parts.filter((p) => rows[p.key]?.decision);
  const missingReason = chosen.filter((p) => rows[p.key].decision !== 'Confirm' && (rows[p.key].comment || '').trim().length < 3);
  const decidedAfter = parts.filter((p) => rows[p.key]?.decision || p.decision).length;

  async function save() {
    setBusy(true);
    setError('');
    try {
      const result = await api.partialDecision(detail.id, {
        parts: chosen.map((p) => ({ key: p.key, decision: rows[p.key].decision, comment: (rows[p.key].comment || '').trim() || null })),
        comment: comment.trim() || null,
      });
      setRows({});
      onSaved(result);
    } catch (err) {
      setError(err.message || 'Не удалось сохранить решение по частям');
    } finally {
      setBusy(false);
    }
  }

  if (!detail.parts?.compound) {
    return <section className="parts-panel"><div className="muted small">Это замечание не составное: решение принимается целиком.</div></section>;
  }
  return (
    <section className="parts-panel" aria-label="Решение по частям">
      <div className="editor-head">
        <h3>Разделить на части — решено {decidedAfter} из {parts.length}</h3>
        <button className="btn tiny" type="button" onClick={onClose}>Закрыть</button>
      </div>
      <div className="muted small">
        Для каждой части выберите решение; для отклонения и уточнения укажите причину. Итоговое решение по замечанию записывается, когда решены все части:
        «Подтвердить», если подтверждена хотя бы одна, иначе «Отклонить» или «Уточнить».
      </div>
      <table className="plain-table parts-table">
        <thead><tr><th>Часть</th><th>Стадии</th><th>Сейчас</th><th>Решение</th><th>Причина</th></tr></thead>
        <tbody>
          {parts.map((p) => {
            const row = rows[p.key] || {};
            const needs = row.decision && row.decision !== 'Confirm';
            return (
              <tr key={p.key} className={row.decision || p.decision ? 'decided' : ''}>
                <td><b>{p.label}</b><div className="muted small">{p.state}</div></td>
                <td className="small">ПД: {formatValue(p.expected)}<br />РД/ИД: {formatValue(p.actual)}</td>
                <td className="small">
                  {p.decision ? <><b>{PART_DECISION_LABELS[p.decision]}</b>{p.comment ? <div className="muted">«{p.comment}»</div> : null}<div className="muted">v{p.version}</div></> : <span className="muted">без решения</span>}
                </td>
                <td>
                  <select className="select" value={row.decision || ''} disabled={locked || busy} onChange={(e) => set(p.key, { decision: e.target.value || null })} aria-label={`Решение по части ${p.label}`}>
                    <option value="">—</option>
                    {Object.keys(DECISION_LABEL_BY_KEY).map((k) => <option key={k} value={k}>{DECISION_LABEL_BY_KEY[k]}</option>)}
                  </select>
                </td>
                <td>
                  <input
                    className="input"
                    value={row.comment || ''}
                    disabled={locked || busy || !row.decision}
                    placeholder={needs ? 'обязательно' : 'необязательно'}
                    onChange={(e) => set(p.key, { comment: e.target.value })}
                    aria-label={`Причина по части ${p.label}`}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <label className="field">
        <span>Общий комментарий к итоговому решению (необязательно)</span>
        <textarea className="textarea" rows={2} value={comment} disabled={locked || busy} onChange={(e) => setComment(e.target.value)} />
      </label>
      {error && <div className="error-banner"><span>{error}</span></div>}
      <div className="row" style={{ gap: 8 }}>
        <button className="btn primary" type="button" onClick={save} disabled={locked || busy || !chosen.length || missingReason.length > 0}>
          {busy ? 'Сохранение…' : `Сохранить решения по частям (${chosen.length})`}
        </button>
        {missingReason.length > 0 && <span className="muted small">укажите причину для: {missingReason.map((p) => p.label).join(', ')}</span>}
        {locked && <span className="muted small">Протокол финализирован — решения заблокированы.</span>}
      </div>
    </section>
  );
}
