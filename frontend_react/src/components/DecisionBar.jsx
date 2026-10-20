import { useEffect, useRef } from 'react';
import { CLARIFY_KINDS, DECISION_OPTIONS, REJECT_REASON_CODES } from '../labels.js';

/**
 * ~3 conscious actions per decision (expert session §16): open the candidate -> choose Confirm / Reject / Clarify
 * (C / R / U) -> save (Enter, Ctrl+Enter inside the comment). A comment is mandatory for a rejection and for a
 * clarification request; a rejection also carries a reason_code.
 */
export default function DecisionBar({ disabledReason, choice, reasonCode, clarifyKind, comment, saving, error, actions, compound, partsDecided, partsTotal, partsOpen, onSplit, onChoose, onReason, onClarifyKind, onComment, onSave, onCancel }) {
  const commentRef = useRef(null);
  const needsComment = choice === 'Reject' || choice === 'Clarification Required';
  const canSave = Boolean(choice) && (!needsComment || comment.trim().length >= 3) && !saving;

  useEffect(() => {
    if (needsComment && commentRef.current) commentRef.current.focus();
  }, [choice, needsComment]);

  if (disabledReason) {
    return <div className="decision-bar disabled"><span className="muted">{disabledReason}</span></div>;
  }

  return (
    <div className="decision-bar">
      <div className="decision-buttons">
        {DECISION_OPTIONS.map((opt) => (
          <button
            key={opt.key}
            type="button"
            className={`btn decision-btn tone-${opt.tone}${choice === opt.key ? ' selected' : ''}`}
            onClick={() => onChoose(opt.key)}
            disabled={saving}
            title={`${opt.label} (клавиша ${opt.hotkey})`}
          >
            <kbd>{opt.hotkey}</kbd> {opt.label}
          </button>
        ))}
        {compound && (
          <button
            type="button"
            className={`btn decision-btn tone-split${partsOpen ? ' selected' : ''}`}
            onClick={onSplit}
            disabled={saving}
            title="Составное замечание: решение по каждой части отдельно (клавиша S)"
          >
            <kbd>S</kbd> Разделить на части{partsTotal ? ` (${partsDecided || 0}/${partsTotal})` : ''}
          </button>
        )}
        <div className="spacer" />
        <span className="muted small" title="Осознанные действия с момента открытия кандидата">действий: {actions}</span>
      </div>

      {choice && (
        <div className="decision-form">
          {choice === 'Reject' && (
            <label className="field inline">
              <span>Причина отклонения</span>
              <select className="select" value={reasonCode} onChange={(e) => onReason(e.target.value)}>
                {REJECT_REASON_CODES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
              </select>
            </label>
          )}
          {choice === 'Clarification Required' && (
            <label className="field inline">
              <span>Что уточнить</span>
              <select className="select" value={clarifyKind} onChange={(e) => onClarifyKind(e.target.value)}>
                {CLARIFY_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
              </select>
            </label>
          )}
          <label className="field">
            <span>
              {choice === 'Reject' ? 'Комментарий — обязателен при отклонении' : choice === 'Clarification Required' ? 'Уточнение — обязательно: что именно не хватает или что неверно' : 'Комментарий (необязательно)'}
            </span>
            <textarea
              ref={commentRef}
              className="textarea"
              rows={2}
              value={comment}
              onChange={(e) => onComment(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && canSave) {
                  e.preventDefault();
                  onSave();
                }
                if (e.key === 'Escape') {
                  e.preventDefault();
                  onCancel();
                }
              }}
              placeholder={choice === 'Reject' ? 'Например: в РД система сохранена, отличается только маркировка' : choice === 'Clarification Required' ? (CLARIFY_KINDS.find((k) => k.value === clarifyKind)?.placeholder || '') : ''}
            />
          </label>
          {error && <div className="error-banner"><span>{error}</span></div>}
          <div className="row" style={{ gap: 8 }}>
            <button className="btn primary" type="button" onClick={onSave} disabled={!canSave}>
              {saving ? 'Сохранение…' : <><kbd>{needsComment ? 'Ctrl+Enter' : 'Enter'}</kbd> Сохранить решение</>}
            </button>
            <button className="btn" type="button" onClick={onCancel} disabled={saving}><kbd>Esc</kbd> Отмена</button>
            {needsComment && comment.trim().length < 3 && <span className="muted small">введите комментарий (от 3 символов)</span>}
          </div>
        </div>
      )}
    </div>
  );
}
