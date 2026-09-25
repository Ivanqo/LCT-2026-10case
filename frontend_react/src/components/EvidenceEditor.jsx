import { useMemo } from 'react';
import { EDIT_ACTION_LABELS, FRAGMENT_STATUS_LABELS, ROLE_LABELS, STAGE_TITLES, label } from '../labels.js';
import { fmtDate, formatBBox, formatValue } from '../utils.js';

const DIFF_FIELDS = [
  ['page', 'страница'],
  ['bbox_norm', 'область'],
  ['extracted_value', 'значение'],
  ['role', 'роль'],
  ['note', 'примечание'],
  ['status', 'статус'],
];

function show(field, value) {
  if (field === 'bbox_norm') return value ? `[${formatBBox(value)}]` : '—';
  if (field === 'role') return ROLE_LABELS[value] || formatValue(value);
  if (field === 'status') return FRAGMENT_STATUS_LABELS[value] || formatValue(value);
  return formatValue(value);
}

function diffLines(edit) {
  if (edit.action === 'ADD') {
    const v = edit.new_value || {};
    return [`${v.file_id || v.file || 'документ'} стр. ${v.page}${v.bbox_norm ? ` [${formatBBox(v.bbox_norm)}]` : ''}${v.extracted_value ? ` «${v.extracted_value}»` : ''}`];
  }
  const before = edit.previous_value || {};
  const after = edit.new_value || {};
  return DIFF_FIELDS.filter(([key]) => JSON.stringify(before[key] ?? null) !== JSON.stringify(after[key] ?? null)).map(
    ([key, title]) => `${title}: ${show(key, before[key])} → ${show(key, after[key])}`,
  );
}

function documentsForStage(documents, stage) {
  const rows = [];
  for (const group of documents?.groups || []) {
    for (const doc of group.documents || []) {
      const ds = String(doc.dataset_stage || '').toUpperCase();
      const internal = { project: 'PD', working: 'RD', as_built: 'ID' }[doc.doc_stage || group.stage];
      const stages = ds === 'RD_ID_MIXED' ? ['RD', 'ID'] : [ds === 'PD' || ds === 'RD' || ds === 'ID' ? ds : internal];
      if (stages.includes(stage)) rows.push(doc);
    }
  }
  return rows;
}

/**
 * Add / exclude / refine evidence fragments (expert session §13). Every change is a new version with the user, the
 * time, the reason, the reference to the source fragment and the previous value; the machine output is never
 * overwritten -- "исключён" hides a machine fragment from the effective evidence, it does not delete it.
 */
export default function EvidenceEditor({ detail, documents, locked, draft, onDraft, onSubmitDraft, busy, error, activeKeys, onFocus, showRemoved, onShowRemoved }) {
  const items = (detail.effective_fragments || []).filter((item) => showRemoved || item.status === 'ACTIVE');
  const edits = detail.edits || [];
  const removedCount = (detail.effective_fragments || []).filter((item) => item.status === 'REMOVED').length;
  const docOptions = useMemo(() => (draft?.mode === 'add' ? documentsForStage(documents, draft.stage) : []), [documents, draft?.mode, draft?.stage]);

  function startAdd() {
    const stage = detail.visible_stages?.[0] || 'PD';
    onDraft({ mode: 'add', stage, document_version_id: '', page: 1, bbox: null, extracted_value: '', role: 'context', note: '', reason: '', drawing: false });
  }

  function startRefine(item) {
    onDraft({
      mode: 'refine', key: item.key, stage: item.stage, document_version_id: item.current.document_version_id,
      page: item.current.page, bbox: item.view_bbox, extracted_value: item.current.extracted_value || '', role: item.current.role || 'context',
      note: item.current.note || '', reason: '', drawing: false, original: item,
    });
    onFocus(item.stage, item.key);
  }

  function startStatus(item, mode) {
    onDraft({ mode, key: item.key, stage: item.stage, reason: '', original: item });
    onFocus(item.stage, item.key);
  }

  const set = (patch) => onDraft({ ...draft, ...patch });
  const reasonOk = (draft?.reason || '').trim().length >= 3;
  const addReady = draft?.mode === 'add' && draft.document_version_id && Number(draft.page) >= 1;

  return (
    <section className="evidence-editor">
      <div className="editor-head">
        <h3>Фрагменты доказательств <span className="muted">({(detail.effective_fragments || []).filter((i) => i.status === 'ACTIVE').length})</span></h3>
        <div className="row" style={{ gap: 10 }}>
          {removedCount > 0 && (
            <label className="row small muted" style={{ gap: 4 }}>
              <input type="checkbox" checked={showRemoved} onChange={(e) => onShowRemoved(e.target.checked)} /> показывать исключённые ({removedCount})
            </label>
          )}
          {!locked && <button className="btn small" type="button" onClick={startAdd} disabled={Boolean(draft)}>+ Добавить фрагмент</button>}
        </div>
      </div>

      <table className="frag-table">
        <thead>
          <tr><th>Стадия</th><th>Источник</th><th>Файл, стр.</th><th>Значение</th><th>Роль</th><th>Версия</th><th /></tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const focused = activeKeys?.[item.stage] === item.key;
            return (
              <tr key={item.key} className={`${item.status !== 'ACTIVE' ? 'removed' : ''}${focused ? ' focused' : ''}`}>
                <td><span className={`stage-chip stage-${item.stage}`}>{STAGE_TITLES[item.stage] || item.stage}</span></td>
                <td>{item.origin === 'INSPECTOR' ? <span className="pill manual-pill">инспектор</span> : <span className="pill">машина</span>}</td>
                <td className="mono small">{formatValue(item.current?.file_id)} • с.{formatValue(item.current?.page)}</td>
                <td className="val">{formatValue(item.current?.extracted_value)}</td>
                <td className="small">{ROLE_LABELS[item.current?.role] || item.current?.role || '—'}</td>
                <td className="small">{item.version ? `v${item.version} • ${label(FRAGMENT_STATUS_LABELS, item.status)}` : 'исходный'}</td>
                <td className="actions">
                  <button className="btn tiny" type="button" onClick={() => onFocus(item.stage, item.key)}>Показать</button>
                  {!locked && item.status === 'ACTIVE' && (
                    <>
                      <button className="btn tiny" type="button" disabled={Boolean(draft)} onClick={() => startRefine(item)}>Уточнить</button>
                      <button className="btn tiny danger" type="button" disabled={Boolean(draft)} onClick={() => startStatus(item, 'remove')}>Исключить</button>
                    </>
                  )}
                  {!locked && item.status === 'REMOVED' && (
                    <button className="btn tiny" type="button" disabled={Boolean(draft)} onClick={() => startStatus(item, 'restore')}>Восстановить</button>
                  )}
                </td>
              </tr>
            );
          })}
          {items.length === 0 && <tr><td colSpan={7} className="muted">Фрагментов нет.</td></tr>}
        </tbody>
      </table>

      {draft && (
        <div className="edit-form">
          <div className="edit-form-title">
            {draft.mode === 'add' && 'Новый фрагмент доказательства'}
            {draft.mode === 'refine' && `Уточнение фрагмента ${draft.key}`}
            {draft.mode === 'remove' && `Исключить фрагмент ${draft.key} из доказательства`}
            {draft.mode === 'restore' && `Вернуть фрагмент ${draft.key} в доказательство`}
            <span className="muted small"> — создаётся новая версия; машинный вывод и исходный файл не изменяются</span>
          </div>

          {draft.mode === 'add' && (
            <div className="form-grid">
              <label className="field">
                <span>Стадия</span>
                <select className="select" value={draft.stage} onChange={(e) => set({ stage: e.target.value, document_version_id: '', bbox: null, drawing: false })}>
                  {['PD', 'RD', 'ID'].map((s) => <option key={s} value={s}>{STAGE_TITLES[s]}</option>)}
                </select>
              </label>
              <label className="field wide">
                <span>Документ</span>
                <select className="select" value={draft.document_version_id} onChange={(e) => set({ document_version_id: Number(e.target.value) || '', bbox: null, drawing: false })}>
                  <option value="">— выберите документ —</option>
                  {docOptions.map((doc) => (
                    <option key={doc.id} value={doc.id}>{doc.file_id ? `${doc.file_id} • ` : ''}{doc.filename}{doc.revision ? ` (ред. ${doc.revision})` : ''}</option>
                  ))}
                </select>
              </label>
            </div>
          )}

          {(draft.mode === 'add' || draft.mode === 'refine') && (
            <>
              <div className="form-grid">
                <label className="field narrow">
                  <span>Страница</span>
                  <input className="input" type="number" min={1} value={draft.page} onChange={(e) => set({ page: Number(e.target.value) || 1, bbox: null })} />
                </label>
                <label className="field wide">
                  <span>Значение (как в документе)</span>
                  <input className="input" value={draft.extracted_value} onChange={(e) => set({ extracted_value: e.target.value })} />
                </label>
                <label className="field narrow">
                  <span>Роль</span>
                  <select className="select" value={draft.role} onChange={(e) => set({ role: e.target.value })}>
                    {Object.entries(ROLE_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                  </select>
                </label>
              </div>
              <div className="row wrap" style={{ gap: 8, marginBottom: 10 }}>
                <button
                  className={`btn small${draft.drawing ? ' primary' : ''}`}
                  type="button"
                  disabled={draft.mode === 'add' && !addReady}
                  onClick={() => set({ drawing: !draft.drawing })}
                >
                  {draft.drawing ? 'Выделение включено — обведите область на панели' : 'Выделить область на странице'}
                </button>
                <span className="muted small mono">{draft.bbox ? `область [${formatBBox(draft.bbox)}]` : 'область не выделена'}</span>
              </div>
              <label className="field">
                <span>Примечание</span>
                <input className="input" value={draft.note} onChange={(e) => set({ note: e.target.value })} />
              </label>
            </>
          )}

          <label className="field">
            <span>Причина изменения — обязательно</span>
            <input className="input" autoFocus={draft.mode === 'remove' || draft.mode === 'restore'} value={draft.reason} onChange={(e) => set({ reason: e.target.value })}
              placeholder={draft.mode === 'remove' ? 'Например: фрагмент относится к другому помещению' : 'Например: рамка захватывает соседнюю ветвь'} />
          </label>
          {error && <div className="error-banner"><span>{error}</span></div>}
          <div className="row" style={{ gap: 8 }}>
            <button
              className={`btn primary${draft.mode === 'remove' ? ' danger-fill' : ''}`}
              type="button"
              disabled={busy || !reasonOk || (draft.mode === 'add' && !addReady)}
              onClick={onSubmitDraft}
            >
              {busy ? 'Сохранение…' : draft.mode === 'remove' ? 'Исключить (новая версия)' : draft.mode === 'restore' ? 'Восстановить (новая версия)' : 'Сохранить версию'}
            </button>
            <button className="btn" type="button" onClick={() => onDraft(null)} disabled={busy}>Отмена</button>
          </div>
        </div>
      )}

      <details className="edit-history" open={edits.length > 0 && edits.length <= 6}>
        <summary>История изменений доказательств ({edits.length})</summary>
        {edits.length === 0 && <div className="muted small">Инспектор ещё не изменял доказательства — показан исходный машинный вывод.</div>}
        {edits.length > 0 && (
          <ol className="history-list">
            {[...edits].reverse().map((edit) => (
              <li key={edit.id}>
                <div>
                  <b>v{edit.version}</b> • {EDIT_ACTION_LABELS[edit.action] || edit.action} <span className="mono small">{edit.entity_key}</span>
                  <span className="muted small"> • {edit.user_login || `user #${edit.user_id}`} • {fmtDate(edit.created_at)}</span>
                </div>
                <div className="small">Причина: «{edit.reason}»</div>
                {diffLines(edit).map((line) => <div key={line} className="muted small mono">{line}</div>)}
                {edit.source_fragment_id && <div className="muted small">исходный машинный фрагмент #{edit.source_fragment_id}{edit.previous_edit_id ? `, предыдущая версия #${edit.previous_edit_id}` : ''}</div>}
              </li>
            ))}
          </ol>
        )}
      </details>
    </section>
  );
}
