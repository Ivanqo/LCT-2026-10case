import { useEffect, useState } from 'react';
import { api } from '../api.js';
import Badge from './Badge.jsx';
import { COMPLETENESS_STATUS_LABELS, COMPLETENESS_TONE, STAGE_TITLES, label } from '../labels.js';

/**
 * Completeness against the EXPECTED manifest, not the number of files (expert session §19): what was uploaded,
 * what was expected, which stages/sections were found, where there is uncertainty and on what basis.
 */
export default function CompletenessPanel({ processId }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [stage, setStage] = useState('ALL');
  const [onlyProblems, setOnlyProblems] = useState(false);

  useEffect(() => {
    setData(null);
    api.completeness(processId).then(setData).catch((err) => setError(err.message || 'Не удалось загрузить комплектность'));
  }, [processId]);

  if (error) return <div className="error-banner"><span>{error}</span></div>;
  if (!data) return <div className="card empty-state"><span className="spinner" /> Сверяю комплект с ожидаемым манифестом…</div>;

  const rows = (data.rows || [])
    .filter((r) => stage === 'ALL' || r.stage === stage)
    .filter((r) => !onlyProblems || (r.status !== 'UPLOADED' && r.status !== 'NOT_APPLICABLE'));

  return (
    <div>
      <section className="card">
        <div className="card-row">
          <h2>Комплектность</h2>
          <div className="row" style={{ gap: 8 }}>
            <Badge tone={COMPLETENESS_TONE[data.status] || 'neutral'}>{label(COMPLETENESS_STATUS_LABELS, data.status)}</Badge>
            <span className="muted small mono">{data.source}</span>
          </div>
        </div>
        <div className="info-banner small">Основание: {data.basis}</div>
        <div className="status-grid top-gap">
          {(data.stages || []).map((s) => (
            <div className="stat-tile" key={s.stage}>
              <div className="n">{s.found_sections}<span className="muted small"> / {s.expected_sections}</span></div>
              <div className="l">{STAGE_TITLES[s.stage]}: разделов найдено / ожидалось • файлов {s.uploaded_files}</div>
            </div>
          ))}
          <div className="stat-tile">
            <div className="n">{data.mixed_stage_files || 0}</div>
            <div className="l">Файлов со смешанной стадией РД/ИД</div>
          </div>
          <div className="stat-tile">
            <div className="n">{data.excluded_files || 0}</div>
            <div className="l">Исключено (дубли, служебные, нечитаемые)</div>
          </div>
        </div>
        {!data.registry_present && (
          <div className="warn-banner small top-gap">Реестр файлов (Перечень ИД 1.1) не загружен: стадии, шифры и редакции определены по именам и штампам — пакет требует уточнения.</div>
        )}
      </section>

      <section className="card">
        <div className="row wrap" style={{ gap: 8, marginBottom: 10 }}>
          {['ALL', 'PD', 'RD', 'ID'].map((s) => (
            <button key={s} type="button" className={`btn small${stage === s ? ' primary' : ''}`} onClick={() => setStage(s)}>{s === 'ALL' ? 'Все стадии' : STAGE_TITLES[s]}</button>
          ))}
          <label className="row small muted" style={{ gap: 4 }}>
            <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} /> только проблемы
          </label>
        </div>
        <table className="plain-table">
          <thead>
            <tr><th>Стадия</th><th>Раздел</th><th>Ожидалось (основание)</th><th>Загружено</th><th>Статус</th><th>Неопределённость</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.stage}-${r.section}`}>
                <td><span className={`stage-chip stage-${r.stage}`}>{STAGE_TITLES[r.stage]}</span></td>
                <td>{r.section}</td>
                <td className="small">{r.expected ? 'да' : 'нет'} <span className="muted">— {r.expected_basis}</span></td>
                <td>
                  {r.uploaded}
                  {r.files?.length > 0 && (
                    <details className="inline-details">
                      <summary className="muted small">файлы</summary>
                      {r.files.map((f) => <div key={f.document_version_id} className="small"><span className="mono">{f.file_id}</span> {f.file}</div>)}
                    </details>
                  )}
                </td>
                <td><Badge tone={COMPLETENESS_TONE[r.status] || 'neutral'}>{label(COMPLETENESS_STATUS_LABELS, r.status)}</Badge></td>
                <td className="small muted">{r.uncertainty || '—'}</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={6} className="muted">Нет строк для выбранного фильтра.</td></tr>}
          </tbody>
        </table>
      </section>
    </div>
  );
}
