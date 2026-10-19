import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';

const blank = () => ({
  document_name: '', document_number: '', section: '', parameter_name: '',
  min_value: '', max_value: '', effective_from: '', effective_to: '', is_active: true,
});

function inputDate(value) {
  if (!value) return '';
  const text = String(value);
  // The API stores naive UTC timestamps. Interpret those as UTC, then render
  // them in the administrator's local datetime-local input.
  const instant = new Date(/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(text) ? text : `${text}Z`);
  return new Date(instant.getTime() - instant.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function serverDateTime(value) {
  return value ? new Date(value).toISOString().replace(/\.\d{3}Z$/, '') : null;
}

function payloadFrom(form) {
  return {
    document_name: form.document_name.trim(),
    document_number: form.document_number.trim() || null,
    section: form.section.trim() || null,
    parameter_name: form.parameter_name.trim() || null,
    min_value: form.min_value === '' ? null : Number(form.min_value),
    max_value: form.max_value === '' ? null : Number(form.max_value),
    effective_from: serverDateTime(form.effective_from),
    effective_to: serverDateTime(form.effective_to),
    is_active: Boolean(form.is_active),
  };
}

function dateLabel(value) {
  return value ? new Date(value).toLocaleString() : 'без даты';
}

export default function NormativeBase() {
  const [rows, setRows] = useState([]);
  const [form, setForm] = useState(blank());
  const [editingId, setEditingId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      setRows(await api.listNormativeBase());
    } catch (err) {
      setError(err.message || 'Не удалось загрузить нормативную базу');
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  function reset() {
    setEditingId(null);
    setForm(blank());
  }

  async function save(event) {
    event.preventDefault();
    setError('');
    setNotice('');
    const body = payloadFrom(form);
    if ((body.min_value !== null || body.max_value !== null) && !body.parameter_name) {
      setError('Укажите код или название параметра для пороговой записи.');
      return;
    }
    if (body.min_value !== null && body.max_value !== null && body.min_value > body.max_value) {
      setError('Минимум не может быть больше максимума.');
      return;
    }
    if (body.effective_from && body.effective_to && body.effective_from > body.effective_to) {
      setError('Дата начала действия должна быть раньше даты окончания.');
      return;
    }
    setBusy(true);
    try {
      if (editingId) await api.updateNormativeBase(editingId, body);
      else await api.createNormativeBase(body);
      reset();
      setNotice(editingId ? 'Нормативная ссылка обновлена.' : 'Нормативная ссылка добавлена.');
      await load();
    } catch (err) {
      setError(err.message || 'Не удалось сохранить запись');
    } finally {
      setBusy(false);
    }
  }

  function edit(row) {
    setEditingId(row.id);
    setForm({
      document_name: row.document_name || '', document_number: row.document_number || '',
      section: row.section || '', parameter_name: row.parameter_name || '',
      min_value: row.min_value ?? '', max_value: row.max_value ?? '',
      effective_from: inputDate(row.effective_from), effective_to: inputDate(row.effective_to),
      is_active: Boolean(row.is_active),
    });
    setNotice('');
    setError('');
  }

  async function toggleActive(row) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await api.updateNormativeBase(row.id, { is_active: !row.is_active });
      setNotice(row.is_active ? 'Запись деактивирована.' : 'Запись активирована.');
      await load();
    } catch (err) {
      setError(err.message || 'Не удалось изменить статус записи');
    } finally {
      setBusy(false);
    }
  }

  const set = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.type === 'checkbox' ? event.target.checked : event.target.value }));

  return (
    <section className="card normative-base">
      <div className="card-row">
        <div>
          <h2>Нормативная база</h2>
          <p className="muted small">Активные записи с порогами участвуют в нормативной проверке. Результат формируется правилом, решение остаётся за инспектором.</p>
        </div>
        <button className="btn small" type="button" onClick={load} disabled={busy}>{busy ? 'Обновляю…' : 'Обновить список'}</button>
      </div>

      {error && <div className="error-banner top-gap"><span>{error}</span></div>}
      {notice && <div className="notice top-gap" role="status">{notice}</div>}

      <form className="normative-form top-gap" onSubmit={save}>
        <h3>{editingId ? 'Изменить нормативную ссылку' : 'Добавить нормативную ссылку'}</h3>
        <div className="normative-fields">
          <label className="field"><span>Документ *</span><input className="input" required maxLength="512" value={form.document_name} onChange={set('document_name')} /></label>
          <label className="field"><span>Номер</span><input className="input" maxLength="128" value={form.document_number} onChange={set('document_number')} /></label>
          <label className="field"><span>Раздел</span><input className="input" maxLength="255" value={form.section} onChange={set('section')} /></label>
          <label className="field"><span>Код / имя параметра</span><input className="input" maxLength="255" value={form.parameter_name} onChange={set('parameter_name')} /></label>
          <label className="field"><span>Минимум</span><input className="input" type="number" step="any" value={form.min_value} onChange={set('min_value')} /></label>
          <label className="field"><span>Максимум</span><input className="input" type="number" step="any" value={form.max_value} onChange={set('max_value')} /></label>
          <label className="field"><span>Действует с</span><input className="input" type="datetime-local" value={form.effective_from} onChange={set('effective_from')} /></label>
          <label className="field"><span>Действует до</span><input className="input" type="datetime-local" value={form.effective_to} onChange={set('effective_to')} /></label>
          <label className="normative-active"><input type="checkbox" checked={form.is_active} onChange={set('is_active')} /> Активна</label>
        </div>
        <div className="row top-gap">
          <button className="btn primary" type="submit" disabled={busy}>{busy ? 'Сохраняю…' : editingId ? 'Сохранить' : 'Добавить'}</button>
          {editingId && <button className="btn" type="button" onClick={reset} disabled={busy}>Отмена</button>}
        </div>
      </form>

      <h3 className="top-gap">Записи ({rows.length})</h3>
      {rows.length === 0 ? <p className="muted">База пуста; прежние проверки остаются без изменений.</p> : (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Документ / раздел</th><th>Параметр</th><th>Порог</th><th>Период</th><th>Статус</th><th>Действия</th></tr></thead>
          <tbody>{rows.map((row) => (
            <tr key={row.id}>
              <td>{row.document_name}{row.document_number ? ` № ${row.document_number}` : ''}{row.section ? <div className="muted small">{row.section}</div> : null}</td>
              <td>{row.parameter_name || '—'}</td>
              <td>{row.min_value ?? '—'} / {row.max_value ?? '—'}</td>
              <td>{dateLabel(row.effective_from)} — {dateLabel(row.effective_to)}</td>
              <td>{row.is_active ? 'Активна' : 'Неактивна'}</td>
              <td><div className="row wrap"><button className="btn small" type="button" onClick={() => edit(row)} disabled={busy}>Изменить</button><button className="btn small" type="button" onClick={() => toggleActive(row)} disabled={busy}>{row.is_active ? 'Деактивировать' : 'Активировать'}</button></div></td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
    </section>
  );
}
