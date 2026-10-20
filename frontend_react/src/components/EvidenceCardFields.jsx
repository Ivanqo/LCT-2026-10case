import Badge from './Badge.jsx';
import { ExplanationBlock, HypothesisCard, IdentityStrip, NoResultNotice, ProtocolEntryPanel } from './CardBlocks.jsx';
import {
  APPROVAL_STATUS_LABELS,
  FINDING_STATUS_LABELS,
  FINDING_TONE,
  INSPECTOR_STATUS_LABELS,
  REVIEW_PRIORITY_LABELS,
  STAGE_TITLES,
  COMPARABILITY_LABELS,
  PROTOCOL_STATUS_LABELS,
  VIOLATION_LABELS,
  cleanRevision,
  humanizeScenario,
  label,
  rejectReasonText,
} from '../labels.js';
import { buildExplanation, fmtConfidence, fmtDate, formatBBox, formatValue } from '../utils.js';

const DECISION_TO_INSPECTOR = { Confirm: 'CONFIRMED', Reject: 'REJECTED', 'Clarification Required': 'CLARIFICATION_REQUIRED' };

function SourceLine({ title, source, stage, sheet }) {
  if (!source || !source.file_id) {
    return (
      <div className="source-line">
        <span className="k">{title}</span>
        <span className="muted">нет доказательства</span>
      </div>
    );
  }
  return (
    <div className="source-line">
      <span className="k">{title}</span>
      <span>
        <span className={`stage-chip stage-${stage}`}>{STAGE_TITLES[stage] || stage}</span>{' '}
        <b className="mono">{source.file_id}</b>
        {source.code && source.code !== source.file_id ? <> • шифр <b className="mono">{source.code}</b></> : null}
        {' • ред. '}<b>{formatValue(cleanRevision(source.revision) || null)}</b>
        {' • '}{label(APPROVAL_STATUS_LABELS, source.approval, 'статус не указан')}
        {' • стр. '}<b>{formatValue(source.page)}</b>
        {' • лист '}<b title={sheet ? sheet.basis : 'В основной надписи страницы обозначение листа не найдено'}>{sheet?.label || '—'}</b>
        {source.bbox_polygon ? <span className="muted mono small" title="Координаты рамки на странице (доли страницы: левый верхний и правый нижний углы)"> • рамка [{formatBBox(source.bbox_polygon)}]</span> : null}
      </span>
    </div>
  );
}

/**
 * The evidence card (expert session §14): ID, object_id, parameter code (M-xxx + legacy), rule, stage, document,
 * cipher, revision, page, bbox, extracted and reference value, description, confidence, the inspector's decision.
 */
export default function EvidenceCardFields({ detail, locked, onNoResultAction, onHypothesisReviewed, protocolKey }) {
  const card = detail.card || {};
  const rule = card.rule || {};
  const lastDecision = card.last_decision;
  const explanation = buildExplanation({ ...detail, expected: card.expected_value ?? detail.expected, actual: card.actual_value ?? detail.actual });
  const quality = detail.quality || {};
  return (
    <div className="evidence-fields">
      <div className="card-title-row">
        <div>
          <div className="row wrap" style={{ gap: 6 }}>
            <span className="pill mono code-pill" title="Код матрицы 1.1">{card.matrix_code || card.parameter_code || '—'}</span>
            {card.legacy_code && card.legacy_code !== card.matrix_code && <span className="pill mono small" title="Прежний код (public_train)">{card.legacy_code}</span>}
            <Badge tone={FINDING_TONE[card.finding_status] || 'neutral'}>{label(FINDING_STATUS_LABELS, card.finding_status)}</Badge>
            {card.review_priority && <span className="pill">{label(REVIEW_PRIORITY_LABELS, card.review_priority)}</span>}
            {quality.low_quality && <span className="pill warn-pill" title={(quality.low_quality_reasons || []).join('; ') || 'LOW_QUALITY'}>низкое качество страницы</span>}
            {quality.low_confidence && <span className="pill warn-pill">низкая уверенность</span>}
            {card.needs_reverification && <span className="pill warn-pill">доказательства изменились — перепроверить</span>}
          </div>
          <h2 className="candidate-title">{card.parameter_name || 'Параметр'}{card.location ? <span className="muted"> — {card.location}</span> : null}</h2>
          <div className="muted small">{card.section || ''}{card.unit ? ` • ед. ${card.unit}` : ''}</div>
        </div>
        <div className="decision-state">
          <div className="muted small">Решение инспектора</div>
          <div className={`decision-value tone-${DECISION_TO_INSPECTOR[lastDecision?.decision] || 'PENDING'}`}>
            {label(INSPECTOR_STATUS_LABELS, card.inspector_status || 'PENDING')}
          </div>
          {lastDecision && <div className="muted small" title={lastDecision.reason_code || undefined}>{fmtDate(lastDecision.created_at)}{lastDecision.reason_code ? ` • ${rejectReasonText(lastDecision.reason_code)}` : ''}</div>}
        </div>
      </div>

      <div className="value-compare">
        <div className="value-box expected">
          <div className="l">Эталон ({STAGE_TITLES[card.expected_stage] || 'ПД'})</div>
          <div className="v">{formatValue(card.expected_value)}</div>
        </div>
        <div className="value-arrow">→</div>
        <div className="value-box actual">
          <div className="l">Извлечено ({STAGE_TITLES[card.actual_stage] || 'РД/ИД'})</div>
          <div className="v">{formatValue(card.actual_value)}</div>
        </div>
        <div className="value-box conf">
          <div className="l" title="Минимум по цепочке: распознавание текста, привязка, правило. Составляющие — в блоке «Основание».">Общая уверенность</div>
          <div className="v">{fmtConfidence(card.confidence)}</div>
        </div>
      </div>

      {explanation && <div className="explanation">{explanation}</div>}

      <NoResultNotice state={detail.no_result} locked={locked} onAction={onNoResultAction} />
      <HypothesisCard hypothesis={detail.hypothesis} groupId={detail.id} locked={locked} onReviewed={onHypothesisReviewed} />
      <IdentityStrip identity={detail.identity} />
      <ExplanationBlock card={card} />

      <dl className="card-grid">
        <dt>Идентификатор находки</dt><dd className="mono">{formatValue(card.finding_id)} <span className="muted">/ {formatValue(card.evidence_group_id)} / #{card.id}</span></dd>
        <dt>Объект (object_id)</dt><dd className="mono">{formatValue(card.object_id)}</dd>
        <dt>Правило проверки</dt>
        <dd>
          <span className="mono">{formatValue(rule.rule_version || rule.source)}</span>
          {rule.comparison_scenario ? <span className="muted" title={`Код сценария: ${rule.comparison_scenario}`}> • сопоставление {humanizeScenario(rule.comparison_scenario)}</span> : null}
          {rule.extractors?.length ? <div className="muted small mono">{rule.extractors.join(', ')}</div> : null}
          {rule.trigger_logic ? <div className="muted small">Триггер: {String(rule.trigger_logic).split('trigger=').pop()}</div> : null}
        </dd>
        <dt>Метка / статус</dt>
        <dd title={[card.violation_label, card.protocol_status, card.comparability_status].filter(Boolean).join(' / ')}>
          {label(VIOLATION_LABELS, card.violation_label)} <span className="muted">• в протоколе: {label(PROTOCOL_STATUS_LABELS, card.protocol_status)} • сопоставимость: {label(COMPARABILITY_LABELS, card.comparability_status)}</span>
        </dd>
      </dl>

      <div className="sources">
        <SourceLine title="Источник эталона" source={card.source_expected} stage={card.expected_stage || card.source_expected?.stage} sheet={card.sheets?.[card.expected_stage || card.source_expected?.stage]} />
        <SourceLine title="Источник факта" source={card.source_actual} stage={card.actual_stage || card.source_actual?.stage} sheet={card.sheets?.[card.actual_stage || card.source_actual?.stage]} />
      </div>

      <ProtocolEntryPanel groupId={detail.id} refreshKey={protocolKey} />
    </div>
  );
}
