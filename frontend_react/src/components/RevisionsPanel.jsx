import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import Badge from './Badge.jsx';
import { APPROVAL_STATUS_LABELS, REVISION_SCOPE_TYPES, REVISION_STATUS_LABELS, STAGE_TITLES, label } from '../labels.js';
import { fmtDate, formatValue } from '../utils.js';

const STATUS_TONE = { CLARIFICATION_REQUIRED: 'warn', RESOLVED: 'ok', RESOLVED_BY_INSPECTOR: 'review' };

/**
 * Edition choice by the inspector (expert session §12-13): where the system cannot tell which edition is
 * authoritative it says so (CLARIFICATION_REQUIRED) instead of guessing; the inspector picks one with a mandatory
 * justification -- a new version with history, the previous choice stays visible.
 */
export default function RevisionsPanel({ processId, locked }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(null);
  const [justification, setJustification] = useState('');
  const [busy, setBusy] = useState(false);
  const [effect, setEffect] = useState(null);

  const load = useCallback(() => {
    api.revisions(processId).then(setData).catch((err) => setError(err.message || 'Не удалось загрузить редакции'));
  }, [processId]);
  useEffect(load, [load]);

  async function submit() {
    setBusy(true);
    setError('');
    try {
      const result = await api.chooseRevision(processId, {
        scope_key: pending.scope.scope_key, document_version_id: pending.candidate.document_version_id, justification: justification.trim(),
      });
      setData(result.revisions);
      setEffect({ scope: pending.scope.scope_key, ...result.effect });
      setPending(null);
      setJustification('');
    } catch (err) {
      setError(err.message || 'Не удалось сохранить выбор редакции');
    } finally {
      setBusy(false);
    }
  }

  if (!data && !error) return <div className="card empty-state"><span className="spinner" /> Анализ редакций…</div>;
  const scopes = data?.scopes || [];

  return (
    <div>
      <section className="card">
        <div className="card-row">
          <h2>Редакции документов</h2>
          <div className="row" style={{ gap: 8 }}>
            {data && <Badge tone={data.open_conflicts ? 'warn' : 'ok'}>{data.open_conflicts ? `Не определено: ${data.open_conflicts}` : 'Неопределённостей нет'}</Badge>}
            <span className="muted small mono" title="Источник правил выбора">{data?.source}</span>
          </div>
        </div>
        <p className="muted small">
          Актуальная редакция определяется по object_id, стадии, шифру, статусу и дате утверждения и цепочке редакций. Если признаки
          противоречивы, вывод о нарушении по затронутым параметрам блокируется до решения инспектора. Устаревшая редакция
          не используется как эталон.
        </p>
        {error && <div className="error-banner"><span>{error}</span></div>}
        {effect && <div className="info-banner small">{effect.note || 'Выбор записан: новая версия с обоснованием и историей.'}</div>}
        {scopes.length === 0 && <div className="empty-state">Конфликтов и цепочек редакций в комплекте не найдено — у каждого раздела одна редакция.</div>}
      </section>

      {scopes.map((scope) => (
        <section className="card revision-scope" key={scope.scope_key}>
          <div className="card-row">
            <div>
              <div className="row wrap" style={{ gap: 6 }}>
                <span className={`stage-chip stage-${scope.stage}`}>{STAGE_TITLES[scope.stage] || scope.stage}</span>
                <b>{scope.scope}</b>
                <span className="muted small">{REVISION_SCOPE_TYPES[scope.type] || scope.type}</span>
              </div>
              <div className="muted small top-gap">Основание: {scope.basis}</div>
            </div>
            <Badge tone={STATUS_TONE[scope.status] || 'neutral'}>{label(REVISION_STATUS_LABELS, scope.status)}</Badge>
          </div>
          <table className="plain-table top-gap">
            <thead>
              <tr><th /><th>Файл</th><th>Шифр</th><th>Ред.</th><th>Статус утверждения</th><th>Дата</th><th /></tr>
            </thead>
            <tbody>
              {scope.candidates.map((c) => {
                const effective = scope.effective_choice_id === c.document_version_id;
                return (
                  <tr key={c.document_version_id} className={effective ? 'chosen' : ''}>
                    <td>{effective ? '✔' : ''}</td>
                    <td><span className="mono small">{formatValue(c.file_id)}</span><div className="small">{c.file}</div></td>
                    <td className="mono small">{formatValue(c.document_code)}</td>
                    <td>{formatValue(c.revision)}</td>
                    <td>{label(APPROVAL_STATUS_LABELS, c.approval_status, 'не указан')}</td>
                    <td className="small">{c.approval_date ? fmtDate(c.approval_date).split(',')[0] : '—'}</td>
                    <td>
                      {!locked && !effective && (
                        <button className="btn tiny" type="button" onClick={() => { setPending({ scope, candidate: c }); setJustification(''); }}>
                          Выбрать актуальной
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {scope.history?.length > 0 && (
            <details className="edit-history">
              <summary>История выбора ({scope.history.length})</summary>
              <ol className="history-list">
                {[...scope.history].reverse().map((h) => (
                  <li key={h.id}>
                    <b>v{h.version}</b> • {h.new_value?.chosen?.file_id || h.new_value?.chosen?.file} <span className="muted small">• {h.user_login || `user #${h.user_id}`} • {fmtDate(h.created_at)}</span>
                    <div className="small">Обоснование: «{h.reason}»</div>
                    <div className="muted small">Было: {h.previous_value?.chosen ? h.previous_value.chosen.file_id : label(REVISION_STATUS_LABELS, h.previous_value?.system_status, 'не определено системой')}</div>
                  </li>
                ))}
              </ol>
            </details>
          )}
        </section>
      ))}

      {pending && (
        <div className="modal-overlay" onClick={() => !busy && setPending(null)}>
          <div className="modal-box wide" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Выбор редакции">
            <h3>Выбрать актуальную редакцию</h3>
            <p className="small"><b>{pending.scope.scope}</b> ({STAGE_TITLES[pending.scope.stage]}): <span className="mono">{pending.candidate.file_id}</span> — {pending.candidate.file}</p>
            <p className="muted small">Остальные редакции этого раздела будут считаться неактуальными для сравнения. Решение сохраняется как новая версия с историей.</p>
            <label className="field">
              <span>Обоснование — обязательно</span>
              <textarea className="textarea" autoFocus value={justification} onChange={(e) => setJustification(e.target.value)}
                placeholder="Например: по реестру заказчика действует изм. 1 от 12.03.2025, письмо № …" />
            </label>
            {error && <div className="error-banner"><span>{error}</span></div>}
            <div className="modal-actions">
              <button className="btn" type="button" onClick={() => setPending(null)} disabled={busy}>Отмена</button>
              <button className="btn primary" type="button" onClick={submit} disabled={busy || justification.trim().length < 3}>{busy ? 'Сохранение…' : 'Сохранить выбор'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
