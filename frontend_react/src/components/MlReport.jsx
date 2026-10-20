import { useCallback, useEffect, useState } from 'react';
import { api, downloadProtectedFile, weeklyMlReportHtmlUrl } from '../api.js';
import { fmtDate, fmtIsoDate } from '../utils.js';

const SEVERITY_LABELS = { HIGH: 'важно', MEDIUM: 'средне', LOW: 'низкий приоритет', INFO: 'справка', WARNING: 'внимание' };

function pct(value) {
  return value === null || value === undefined ? '—' : `${Math.round(value * 100)} %`;
}

function signed(value, percent) {
  if (value === null || value === undefined) return '—';
  const n = percent ? Math.round(value * 100) : value;
  return `${n > 0 ? '+' : ''}${n}${percent ? ' п. п.' : ''}`;
}

function Delta({ value, percent, goodWhenUp = true }) {
  if (value === null || value === undefined || value === 0) return <span className="muted">{signed(value, percent)}</span>;
  const good = goodWhenUp ? value > 0 : value < 0;
  return <span className={good ? 'delta-up' : 'delta-down'}>{signed(value, percent)}</span>;
}

function BucketTable({ title, rows, nameHeader }) {
  return (
    <>
      <h3>{title}</h3>
      {rows.length === 0 ? <p className="muted">Нет решений за неделю.</p> : (
        <table className="data-table">
          <thead>
            <tr><th>{nameHeader}</th><th>Решений</th><th>Подтв.</th><th>Откл.</th><th>Уточн.</th><th>Доля подтверждённых</th><th>Δ решений</th><th>Δ доли</th></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.name}>
                <td>{row.name}</td><td>{row.decisions}</td><td>{row.confirmed}</td><td>{row.rejected}</td><td>{row.clarified}</td>
                <td>{pct(row.precision_by_inspectors)}</td>
                <td><Delta value={row.decisions_change} goodWhenUp /></td>
                <td><Delta value={row.precision_change} percent /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

/** Module 10: weekly report for ML engineers (inspector decisions by reason, section and method; recommendations; retraining log). */
export default function MlReport({ projectId = null }) {
  const [report, setReport] = useState(null);
  const [savedReports, setSavedReports] = useState([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [end, setEnd] = useState('');

  const load = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const [current, saved] = await Promise.all([
        api.weeklyMlReport({ end, project_id: projectId }),
        // The scheduler persists one snapshot per organization. Keep the
        // project filter on the on-demand report, but list organization
        // snapshots regardless of the currently selected project.
        api.savedWeeklyMlReports(),
      ]);
      setReport(current);
      setSavedReports(saved);
    } catch (err) {
      setError(err.message || 'Не удалось загрузить отчёт');
    } finally {
      setBusy(false);
    }
  }, [end, projectId]);

  useEffect(() => { load(); }, [load]);

  const totals = report?.totals;
  return (
    <section className="card ml-report">
      <div className="card-row">
        <h2>Еженедельный отчёт для ML-инженеров</h2>
        <div className="row wrap" style={{ gap: 8 }}>
          <label className="field compact" style={{ margin: 0 }}>
            <span>Последний день недели</span>
            <input className="input" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          </label>
          <button className="btn small" type="button" onClick={load} disabled={busy}>{busy ? 'Обновляю…' : 'Обновить'}</button>
          <button className="btn small" type="button" disabled={!report}
            onClick={() => downloadProtectedFile(weeklyMlReportHtmlUrl({ end }), 'ml_weekly_report.html').catch((e) => setError(e.message))}>
            Скачать (HTML)
          </button>
        </div>
      </div>
      {error && <div className="error-banner top-gap"><span>{error}</span></div>}
      <h3 className="top-gap">Сохранённые отчёты организации</h3>
      {savedReports.length === 0 ? <p className="muted">Планировщик пока не сохранил отчёты.</p> : (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Период</th><th>Сохранён</th><th>Решений</th><th>Действие</th></tr></thead>
          <tbody>{savedReports.map((item) => (
            <tr key={item.id}>
              <td>{fmtIsoDate(item.period?.from)} — {fmtIsoDate(item.period?.to)}</td><td>{fmtDate(item.saved_at)}</td>
              <td>{item.report?.totals?.decisions ?? '—'}</td>
              <td><button className="btn small" type="button" onClick={() => setReport(item.report)}>Открыть</button></td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      {report && (
        <>
          <p className="muted small">
            Период: {fmtIsoDate(report.period.from)} — {fmtIsoDate(report.period.to)}; прошлая неделя: {fmtIsoDate(report.period.previous_from)} — {fmtIsoDate(report.period.previous_to)}.
          </p>
          {report.data_sufficiency.note && <div className="notice" role="status">{report.data_sufficiency.note}</div>}

          <h3>Итоги недели</h3>
          <table className="data-table">
            <thead><tr><th>Решений</th><th>Подтверждено</th><th>Отклонено</th><th>Уточнение</th><th>Доля подтверждённых (95 % Уилсон)</th><th>К прошлой неделе</th></tr></thead>
            <tbody>
              <tr>
                <td>{totals.decisions}</td><td>{totals.confirmed}</td><td>{totals.rejected}</td><td>{totals.clarified}</td>
                <td>
                  {pct(totals.precision_by_inspectors)}
                  {totals.precision_wilson95 ? ` (${pct(totals.precision_wilson95[0])}–${pct(totals.precision_wilson95[1])})` : ''}
                  <span className="muted small"> n={totals.judged}</span>
                </td>
                <td>решений <Delta value={report.change.decisions} />, доля <Delta value={report.change.precision_by_inspectors} percent /></td>
              </tr>
            </tbody>
          </table>

          <BucketTable title="По разделам Матрицы" rows={report.by_section} nameHeader="Раздел" />
          <BucketTable title="По методам" rows={report.by_method} nameHeader="Метод" />

          <h3>Причины отклонения</h3>
          {report.top_reject_reasons.length === 0 ? <p className="muted">Отклонений за неделю нет.</p> : (
            <table className="data-table">
              <thead><tr><th>Причина</th><th>Отклонений</th><th>Доля</th><th>Прошлая неделя</th><th>Δ</th></tr></thead>
              <tbody>
                {report.top_reject_reasons.map((row) => (
                  <tr key={row.reason_code}>
                    <td title={`Код причины: ${row.reason_code}`}>{row.title || row.reason_code}</td><td>{row.count}</td><td>{pct(row.share)}</td><td>{row.previous_week}</td>
                    <td><Delta value={row.change} goodWhenUp={false} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h3>Рекомендации по донастройке</h3>
          {report.recommendations.length === 0 ? (
            <p className="muted">{report.data_sufficiency.enough ? 'Ни одно правило не сработало.' : 'Данных мало: рекомендации не формируются.'}</p>
          ) : (
            <ul>
              {report.recommendations.map((item, index) => (
                <li key={`${item.rule}-${index}`}><b>[{SEVERITY_LABELS[item.severity] || item.severity}]</b> {item.text} <span className="muted small">({item.rule})</span></li>
              ))}
            </ul>
          )}

          <h3>Журнал переобучения</h3>
          {report.retraining.recent.length === 0 ? <p className="muted">{report.retraining.note}</p> : (
            <table className="data-table">
              <thead><tr><th>Дата</th><th>Набор данных</th><th>Кандидат модели</th><th>Шлюз</th><th>Утверждение</th><th>Причины</th></tr></thead>
              <tbody>
                {report.retraining.recent.map((row) => (
                  <tr key={row.id}>
                    <td className="small">{fmtDate(row.created_at)}</td><td>{row.dataset_version}</td><td>{row.candidate_model_version || '—'}</td>
                    <td>{row.gate_status}</td><td>{row.approval_status}</td><td className="small">{(row.gate_reasons || []).join('; ') || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="muted small top-gap">{report.definitions.precision_by_inspectors}.</p>
        </>
      )}
    </section>
  );
}
