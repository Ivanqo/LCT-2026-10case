import { useState } from 'react';
import Badge from './Badge.jsx';
import DecisionDialog from './DecisionDialog.jsx';
import PageViewerModal from './PageViewerModal.jsx';
import {
  FINDING_STATUS_LABELS,
  FINDING_TONE,
  INSPECTOR_STATUS_LABELS,
  DATASET_STAGE_LABELS,
  APPROVAL_STATUS_LABELS,
  REVIEW_PRIORITY_LABELS,
  label,
} from '../labels.js';
import { formatValue, formatBBox, formatDelta, buildExplanation, fmtDate } from '../utils.js';

export default function EvidenceCard({ group, locked, onDecide }) {
  const [dialogMode, setDialogMode] = useState(null);
  const [pendingDecision, setPendingDecision] = useState(null);
  const [viewerFragment, setViewerFragment] = useState(null);
  const [error, setError] = useState('');

  const param = group.parameter || {};
  const paramCode = param.matrix_code && param.scoring_code && param.matrix_code !== param.scoring_code
    ? `${param.matrix_code} / ${param.scoring_code}`
    : (param.matrix_code || param.code || 'PARAM');
  const explanation = buildExplanation(group);
  const deltaText = formatDelta(group.delta);
  const fragments = group.fragments || [];
  const decisions = group.decisions || [];

  async function runDecision(decision, extra) {
    setPendingDecision(decision);
    setError('');
    try {
      await onDecide(group.id, decision, extra?.reasonCode, extra?.comment);
      setDialogMode(null);
    } catch (err) {
      setError(err.message || 'Не удалось сохранить решение');
    } finally {
      setPendingDecision(null);
    }
  }

  return (
    <article className={`evidence-card${locked ? ' locked' : ''}`}>
      <div className="evidence-head">
        <div>
          <div className="row wrap" style={{ gap: 8 }}>
            <span className="pill mono">{paramCode}</span>
            <Badge tone={FINDING_TONE[group.finding_status] || 'neutral'}>{label(FINDING_STATUS_LABELS, group.finding_status)}</Badge>
            {group.review_priority && <span className="pill">{label(REVIEW_PRIORITY_LABELS, group.review_priority, group.review_priority)}</span>}
          </div>
          <h3>{param.name || 'Параметр'}</h3>
          <div className="muted small">
            {group.entity_name || group.object_id || 'Объект'}
            {param.section ? ` • ${param.section}` : ''}
          </div>
        </div>
        <div className="muted small" style={{ textAlign: 'right' }}>
          {decisions.length > 0 ? label(INSPECTOR_STATUS_LABELS, group.inspector_status) : 'Ожидает решения'}
        </div>
      </div>

      <div className="evidence-values">
        <div className="value-box">
          <div className="l">Ожидается (ПД)</div>
          <div className="v">{formatValue(group.expected)}</div>
        </div>
        <div className="value-box">
          <div className="l">Фактически (РД/ИД)</div>
          <div className="v">{formatValue(group.actual)}</div>
        </div>
        {deltaText && (
          <div className="value-box">
            <div className="l">Результат сравнения</div>
            <div className="v">{deltaText}</div>
          </div>
        )}
      </div>

      {explanation && <div className="explanation">{explanation}</div>}

      <div className="fragments">
        {fragments.length === 0 && <div className="muted small">Фрагменты доказательств не найдены.</div>}
        {fragments.map((fragment) => {
          const bbox = formatBBox(fragment.bbox_normalized || fragment.bbox);
          return (
            <div className="fragment" key={fragment.id}>
              <div className="fragment-head">
                <span className="pill">{label(DATASET_STAGE_LABELS, fragment.dataset_stage, fragment.stage)}</span>
                <span className="pill small">лист {formatValue(fragment.page)}</span>
                {fragment.revision && <span className="pill small">ред. {fragment.revision}</span>}
                <span className="pill small">{label(APPROVAL_STATUS_LABELS, fragment.approval_status)}</span>
              </div>
              <div className="muted small">
                {fragment.file || fragment.file_id || 'Файл не определён'}
                {fragment.document_code ? ` • ${fragment.document_code}` : ''}
              </div>
              {fragment.extracted_value && <div className="fragment-value">{fragment.extracted_value}</div>}
              {bbox && <div className="muted small">bbox: [{bbox}]</div>}
              {fragment.context && <div className="fragment-context">«{fragment.context.slice(0, 200)}»</div>}
              {fragment.file_id && fragment.page && (
                <button className="btn small top-gap" type="button" onClick={() => setViewerFragment(fragment)}>
                  Показать на странице (bbox)
                </button>
              )}
            </div>
          );
        })}
      </div>

      {error && <div className="error-banner top-gap"><span>{error}</span></div>}

      {!locked && (
        <div className="actions-row">
          <button
            className="btn small primary"
            type="button"
            disabled={pendingDecision !== null}
            onClick={() => runDecision('Confirm')}
          >
            {pendingDecision === 'Confirm' ? 'Сохранение…' : 'Подтвердить нарушение'}
          </button>
          <button
            className="btn small"
            type="button"
            disabled={pendingDecision !== null}
            onClick={() => setDialogMode('Reject')}
          >
            Нарушения нет
          </button>
          <button
            className="btn small"
            type="button"
            disabled={pendingDecision !== null}
            onClick={() => setDialogMode('Clarification Required')}
          >
            Требуется уточнение
          </button>
        </div>
      )}

      {decisions.length > 0 && (
        <div className="decision-log">
          {decisions.map((d) => (
            <div className="item" key={d.id}>
              {fmtDate(d.created_at)} — {label(INSPECTOR_STATUS_LABELS, { Confirm: 'CONFIRMED', Reject: 'REJECTED', 'Clarification Required': 'CLARIFICATION_REQUIRED' }[d.decision])}
              {d.reason_code ? ` • ${d.reason_code}` : ''}
              {d.comment ? `: «${d.comment}»` : ''}
            </div>
          ))}
        </div>
      )}

      {dialogMode && (
        <DecisionDialog
          mode={dialogMode}
          onCancel={() => setDialogMode(null)}
          onSubmit={(extra) => runDecision(dialogMode, extra)}
        />
      )}

      {viewerFragment && <PageViewerModal fragment={viewerFragment} onClose={() => setViewerFragment(null)} />}
    </article>
  );
}
