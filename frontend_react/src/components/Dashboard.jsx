import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import { fmtDate } from '../utils.js';

const COLOR_ORDER = ['RED', 'YELLOW', 'GREEN', 'GRAY'];
const COLOR_NAMES = { RED: 'Красный', YELLOW: 'Жёлтый', GREEN: 'Зелёный', GRAY: 'Серый' };
const PROCESS_STATUS_NAMES = {
  PENDING: 'ожидает', QUEUED: 'в очереди', PROCESSING: 'идёт проверка', PARSING: 'разбор документов', READY: 'готово к проверке',
  VERIFYING: 'идёт верификация', COMPLETED: 'проверка завершена', FINALIZED: 'протокол финализирован', FAILED: 'ошибка',
};

/** Module 7: objects / checks with a colour indication. The colour rule comes from the server (`rules`) and is shown as a hint. */
export default function Dashboard({ projectId, onOpen }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [section, setSection] = useState('');
  const [colors, setColors] = useState([]);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [onlyProject, setOnlyProject] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      setData(await api.dashboard({
        section, status: colors.join(','), date_from: dateFrom, date_to: dateTo, project_id: onlyProject ? projectId : '',
      }));
    } catch (err) {
      setError(err.message || 'Не удалось загрузить панель');
    } finally {
      setBusy(false);
    }
  }, [section, colors, dateFrom, dateTo, onlyProject, projectId]);

  useEffect(() => { load(); }, [load]);

  function toggleColor(color) {
    setColors((current) => (current.includes(color) ? current.filter((c) => c !== color) : [...current, color]));
  }

  const rules = data?.rules || {};
  return (
    <section className="card dashboard">
      <div className="card-row">
        <h2>Объекты и проверки</h2>
        <button className="btn small" type="button" onClick={load} disabled={busy}>{busy ? 'Обновляю…' : 'Обновить'}</button>
      </div>

      <div className="dash-summary" role="group" aria-label="Сводка по цветам">
        {COLOR_ORDER.map((color) => (
          <button
            key={color}
            type="button"
            className={`dash-tile ${color.toLowerCase()}${colors.includes(color) ? ' on' : ''}`}
            title={rules[color] || ''}
            aria-pressed={colors.includes(color)}
            onClick={() => toggleColor(color)}
          >
            <span className="dash-dot" />
            <b>{data?.summary?.[color] ?? 0}</b>
            <span>{COLOR_NAMES[color]}</span>
          </button>
        ))}
      </div>

      <div className="dash-filters row wrap top-gap">
        <label className="field compact">
          <span>Раздел Матрицы</span>
          <select className="select" value={section} onChange={(e) => setSection(e.target.value)}>
            <option value="">Все разделы</option>
            {(data?.filters?.sections || []).map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        <label className="field compact">
          <span>Проверка с (дата создания)</span>
          <input className="input" type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
        </label>
        <label className="field compact">
          <span>по</span>
          <input className="input" type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
        </label>
        <label className="dash-check">
          <input type="checkbox" checked={onlyProject} onChange={(e) => setOnlyProject(e.target.checked)} />
          <span>Только текущий проект</span>
        </label>
        {(section || colors.length || dateFrom || dateTo || onlyProject) && (
          <button className="btn small" type="button" onClick={() => { setSection(''); setColors([]); setDateFrom(''); setDateTo(''); setOnlyProject(false); }}>
            Сбросить фильтры
          </button>
        )}
      </div>

      <details className="dash-rules top-gap">
        <summary>Как определяется цвет</summary>
        <ul>
          {COLOR_ORDER.map((color) => <li key={color}>{rules[color]}</li>)}
        </ul>
        <p className="muted small">Цвет не хранится: он считается по текущим находкам и решениям инспектора. Серый приоритетнее остальных, красный — жёлтого.</p>
      </details>

      {error && <div className="error-banner top-gap"><span>{error}</span></div>}
      {data && data.items.length === 0 && !busy && (
        <div className="empty-state top-gap">Проверок по этим условиям нет.</div>
      )}
      {data && data.items.length > 0 && (
        <table className="data-table top-gap">
          <thead>
            <tr>
              <th>Цвет</th><th>Объект</th><th>Проект</th><th>Состояние проверки</th>
              <th title="Подтверждено инспектором">Подтв.</th>
              <th title="Нарушения-кандидаты, ожидающие решения">Кандидаты</th>
              <th title="Предположения для верификации">Предп.</th>
              <th title="Свободные гипотезы, ожидающие решения">Гипотезы</th>
              <th>Создана</th><th />
            </tr>
          </thead>
          <tbody>
            {data.items.map((row) => (
              <tr key={row.process_id} className={`dash-row ${row.color.toLowerCase()}`} onClick={() => onOpen(row.project_id, row.process_id)}>
                <td>
                  <span className={`dash-pill ${row.color.toLowerCase()}`} title={`${rules[row.color] || ''} Причина: ${row.reason_text}.`}>
                    <span className="dash-dot" />{row.color_title}
                  </span>
                </td>
                <td><b>{row.object_name || '—'}</b><div className="muted small">{row.reason_text}</div></td>
                <td>{row.project_name || row.project_id}</td>
                <td>{PROCESS_STATUS_NAMES[row.process_status] || row.process_status}</td>
                <td>{row.counts.confirmed}</td>
                <td>{row.counts.violation_candidates}</td>
                <td>{row.counts.suggestions}</td>
                <td>{row.counts.hypotheses}</td>
                <td className="small">{fmtDate(row.created_at)}</td>
                <td><button className="btn tiny" type="button" onClick={(e) => { e.stopPropagation(); onOpen(row.project_id, row.process_id); }}>Открыть рабочее место</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
