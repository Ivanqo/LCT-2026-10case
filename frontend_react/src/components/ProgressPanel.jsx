import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { STAGE_TITLES } from '../labels.js';

/**
 * Progress of the check (checklist 10): documents and pages processed per stage, skips with reasons, candidates found
 * and reviewed; a partial result is marked incomplete and says why. Refreshes when `refreshKey` changes.
 */
export default function ProgressPanel({ processId, refreshKey }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!processId) return undefined;
    let cancelled = false;
    api.progress(processId)
      .then((d) => { if (!cancelled) { setData(d); setError(''); } })
      .catch((err) => { if (!cancelled) setError(err.message || 'Не удалось загрузить прогресс'); });
    return () => { cancelled = true; };
  }, [processId, refreshKey]);

  if (error) return <div className="error-banner top-gap"><span>{error}</span></div>;
  if (!data) return null;
  const pct = data.candidates_found ? Math.round((100 * data.candidates_reviewed) / data.candidates_found) : 100;
  return (
    <div className="progress-panel top-gap">
      <div className="row wrap" style={{ gap: 12, alignItems: 'center' }}>
        <b>Прогресс проверки</b>
        <span className="small">рассмотрено кандидатов: <b>{data.candidates_reviewed}</b> из <b>{data.candidates_found}</b></span>
        <div className="progress-track" style={{ width: 160 }} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className="progress-bar" style={{ width: `${pct}%` }} />
        </div>
        {data.stages.map((s) => (
          <span key={s.stage} className="small" title={s.skips.map((k) => `${k.label}: ${k.count}`).join('\n') || 'пропусков нет'}>
            <span className={`stage-chip mini stage-${s.stage}`}>{STAGE_TITLES[s.stage]}</span>{' '}
            {s.uploaded
              ? <>документов {s.documents_scanned}/{s.documents_scannable || s.documents_total}{s.pages_total ? `, листов ${s.pages_scanned}/${s.pages_total}` : ''}</>
              : s.stage_unconfirmed_documents
                ? <span className="muted">стадия не подтверждена ({s.stage_unconfirmed_documents} файл. РД/ИД)</span>
                : <span className="muted">не загружена</span>}
          </span>
        ))}
        <span className={`pill ${data.incomplete ? 'warn-pill' : 'manual-pill'}`}>{data.incomplete ? 'Охват неполный' : 'Охват полный'}</span>
        <button className="btn tiny" type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open}>{open ? 'Скрыть подробности' : 'Подробнее'}</button>
      </div>
      {open && (
        <div className="small top-gap">
          {data.incomplete && (
            <div className="progress-incomplete">
              <b>Частичный результат.</b> {data.note}
              <ul>{data.incomplete_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
            </div>
          )}
          {data.coverage_summary && <div className="muted">{data.coverage_summary}</div>}
          <div className="top-gap"><b>Параметры без результата:</b> {data.parameters_without_result} из {data.parameters_total}</div>
          {data.skipped_parameters.length > 0 && (
            <ul>{data.skipped_parameters.map((s) => <li key={s.reason}>{s.label} — {s.count}</li>)}</ul>
          )}
          {data.stages.some((s) => s.skips.length) && (
            <ul>{data.stages.flatMap((s) => s.skips.map((k) => <li key={`${s.stage}${k.reason}`}>{STAGE_TITLES[s.stage]}: {k.label} — {k.count}</li>))}</ul>
          )}
        </div>
      )}
    </div>
  );
}
