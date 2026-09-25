import { useState } from 'react';
import { REJECT_REASON_CODES } from '../labels.js';

export default function DecisionDialog({ mode, onCancel, onSubmit }) {
  const isReject = mode === 'Reject';
  const [reasonCode, setReasonCode] = useState(isReject ? REJECT_REASON_CODES[0].value : '');
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);

  const title = isReject ? 'Отклонить: несоответствие не подтверждено' : 'Запросить уточнение';
  const canSubmit = isReject ? true : comment.trim().length > 0;

  async function submit() {
    setBusy(true);
    try {
      await onSubmit({ reasonCode: isReject ? reasonCode : null, comment: comment.trim() || null });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-box" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        {isReject && (
          <label className="field">
            <span>Причина отклонения</span>
            <select className="select" value={reasonCode} onChange={(e) => setReasonCode(e.target.value)}>
              {REJECT_REASON_CODES.map((r) => (
                <option key={r.value} value={r.value}>{r.label}</option>
              ))}
            </select>
          </label>
        )}
        <label className="field">
          <span>{isReject ? 'Комментарий (необязательно)' : 'Что нужно уточнить'}</span>
          <textarea
            className="textarea"
            autoFocus={!isReject}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder={isReject ? 'Например: сравнение проведено с устаревшей редакцией РД' : 'Опишите, что должен уточнить исполнитель или следующий инспектор'}
          />
        </label>
        <div className="modal-actions">
          <button className="btn" type="button" onClick={onCancel} disabled={busy}>Отмена</button>
          <button className="btn primary" type="button" onClick={submit} disabled={busy || !canSubmit}>
            {busy ? 'Сохранение…' : 'Сохранить решение'}
          </button>
        </div>
      </div>
    </div>
  );
}
