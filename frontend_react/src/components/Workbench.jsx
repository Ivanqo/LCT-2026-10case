import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import CandidateQueue, { BulkDialog, filterQueue } from './CandidateQueue.jsx';
import DecisionBar from './DecisionBar.jsx';
import EvidenceCardFields from './EvidenceCardFields.jsx';
import EvidenceEditor from './EvidenceEditor.jsx';
import PartsPanel from './PartsPanel.jsx';
import StagePanels, { ZOOM_MAX, ZOOM_MIN } from './StagePanels.jsx';
import SupplementDialog from './SupplementDialog.jsx';
import { CLARIFY_KINDS, DECIDABLE_STATUSES, DECISION_OPTIONS, FINDING_STATUS_LABELS, INSPECTOR_STATUS_LABELS, REJECT_REASON_CODES, label } from '../labels.js';
import { fmtDate, isTypingTarget } from '../utils.js';

/** File identity for a panel in draw mode: the refined fragment's own, or the chosen document's (add). */
function documentMeta(documents, draft) {
  if (draft.mode === 'refine' && draft.original?.current) {
    const { file, file_id, document_code, revision, approval_status, file_sha256 } = draft.original.current;
    return { file, file_id, document_code, revision, approval_status, file_sha256 };
  }
  for (const group of documents?.groups || []) {
    const doc = (group.documents || []).find((d) => Number(d.id) === Number(draft.document_version_id));
    if (doc) {
      return { file: doc.filename, file_id: doc.file_id, document_code: doc.document_code, revision: doc.revision,
        approval_status: doc.approval_status, file_sha256: doc.file_hash };
    }
  }
  return {};
}

const DEFAULT_FILTERS = { mode: 'candidates', status: 'ALL', section: 'ALL', search: '', threshold: 0.75 };

/** What survives a page reload (the decisions themselves are in the database): open record, filters, view modes. */
function loadViewState(processId) {
  try {
    return JSON.parse(localStorage.getItem(`case10_workbench_${processId}`) || 'null') || {};
  } catch {
    return {};
  }
}

function saveViewState(processId, state) {
  try {
    localStorage.setItem(`case10_workbench_${processId}`, JSON.stringify(state));
  } catch {
    /* storage may be unavailable: the view simply starts fresh */
  }
}

const cardSignature = (i) => [i.finding_status, i.expected, i.actual, i.needs_reverification, (i.stages || []).join(','), i.fragments, i.inspector_status].join('|');

const DECISION_BY_CODE = Object.fromEntries(DECISION_OPTIONS.map((o) => [`Key${o.hotkey}`, o.key]));
const INSPECTOR_BY_DECISION = { Confirm: 'CONFIRMED', Reject: 'REJECTED', 'Clarification Required': 'CLARIFICATION_REQUIRED' };

/**
 * The inspector's single candidate window (expert session §14-17): queue on the left, the evidence card, the
 * decision bar and the synchronized ПД/РД/ИД panels on the right. Every decision reports how many conscious actions
 * and how much time it took from opening the candidate (DECISION_UI_METRICS in the audit log, for the pitch).
 */
export default function Workbench({ projectId, processId, locked, onChanged, onOpenSection }) {
  const [summary, setSummary] = useState(null);
  const [summaryError, setSummaryError] = useState('');
  const [filters, setFilters] = useState(() => ({ ...DEFAULT_FILTERS, ...(loadViewState(processId).filters || {}) }));
  const [pair, setPair] = useState(() => loadViewState(processId).pair || 'PD_RD_ID');
  const [sync, setSync] = useState(() => loadViewState(processId).sync !== false);
  const [showFrames, setShowFrames] = useState(() => loadViewState(processId).showFrames !== false);
  const [partsOpen, setPartsOpen] = useState(false);
  const [supplement, setSupplement] = useState(null);
  const [changedIds, setChangedIds] = useState(() => new Map());
  const [supplementNote, setSupplementNote] = useState('');
  const [clarifyKind, setClarifyKind] = useState(CLARIFY_KINDS[0].value);
  const pendingSelect = useRef(loadViewState(processId).selectedId || null);
  const snapshot = useRef(null);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState('');
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [checked, setChecked] = useState(new Set());
  const [bulk, setBulk] = useState(null);
  const [zoom, setZoom] = useState('auto');
  const [activeKeys, setActiveKeys] = useState({});
  const [showRemoved, setShowRemoved] = useState(false);
  const [documents, setDocuments] = useState(null);
  const [draft, setDraft] = useState(null);
  const [draftBusy, setDraftBusy] = useState(false);
  const [draftError, setDraftError] = useState('');
  const [choice, setChoice] = useState(null);
  const [reasonCode, setReasonCode] = useState(REJECT_REASON_CODES[0].value);
  const [comment, setComment] = useState('');
  const [saving, setSaving] = useState(false);
  const [decisionError, setDecisionError] = useState('');
  const [actions, setActions] = useState(0);
  const metrics = useRef({ openedAt: 0, actions: 0, via: 'mouse' });
  const detailToken = useRef(0);
  const thresholdInit = useRef(false);

  const refreshSummary = useCallback(async () => {
    if (!processId) return null;
    try {
      const data = await api.workbench(processId);
      setSummary(data);
      setSummaryError('');
      if (!thresholdInit.current && typeof data.low_confidence_threshold === 'number') {
        thresholdInit.current = true;
        setFilters((f) => ({ ...f, threshold: data.low_confidence_threshold }));
      }
      return data;
    } catch (err) {
      setSummaryError(err.message || 'Не удалось загрузить очередь кандидатов');
      return null;
    }
  }, [processId]);

  useEffect(() => {
    const stored = loadViewState(processId);
    setSummary(null);
    setSelectedId(null);
    setDetail(null);
    setChecked(new Set());
    setChangedIds(new Map());
    setSupplementNote('');
    if (stored.filters) setFilters({ ...DEFAULT_FILTERS, ...stored.filters });
    pendingSelect.current = stored.selectedId || null;
    thresholdInit.current = Boolean(stored.filters);
    refreshSummary();
  }, [refreshSummary, processId]);

  useEffect(() => {
    if (!processId) return;
    saveViewState(processId, { selectedId, filters, pair, sync, showFrames });
  }, [processId, selectedId, filters, pair, sync, showFrames]);

  useEffect(() => {
    if (!projectId) return;
    api.listCase10Documents(projectId).then(setDocuments).catch(() => setDocuments(null));
  }, [projectId]);

  const rows = useMemo(() => filterQueue(summary?.items || [], filters), [summary, filters]);

  const loadDetail = useCallback(async (id) => {
    const token = ++detailToken.current;
    setLoadingDetail(true);
    setDetailError('');
    try {
      const data = await api.groupWorkbench(id);
      if (token === detailToken.current) setDetail(data);
    } catch (err) {
      if (token === detailToken.current) setDetailError(err.message || 'Не удалось загрузить карточку');
    } finally {
      if (token === detailToken.current) setLoadingDetail(false);
    }
  }, []);

  const openCandidate = useCallback((id, via = 'mouse') => {
    if (!id) return;
    setSelectedId(id);
    metrics.current = { openedAt: performance.now(), actions: 1, via };
    setActions(1);
    setChoice(null);
    setComment('');
    setReasonCode(REJECT_REASON_CODES[0].value);
    setClarifyKind(CLARIFY_KINDS[0].value);
    setPartsOpen(false);
    setDecisionError('');
    setDraft(null);
    setDraftError('');
    setActiveKeys({});
    loadDetail(id);
  }, [loadDetail]);

  useEffect(() => {
    if (!rows.length) return;
    if (!selectedId || !(summary?.items || []).some((i) => i.id === selectedId)) {
      const restored = pendingSelect.current && (summary?.items || []).some((i) => i.id === pendingSelect.current) ? pendingSelect.current : null;
      pendingSelect.current = null;
      openCandidate(restored || rows[0].id, 'auto');
    }
  }, [rows, selectedId, summary, openCandidate]);

  function bump() {
    metrics.current.actions += 1;
    setActions(metrics.current.actions);
  }

  function move(step, via = 'keyboard') {
    if (!rows.length) return;
    const index = rows.findIndex((r) => r.id === selectedId);
    const next = rows[Math.min(rows.length - 1, Math.max(0, (index < 0 ? 0 : index) + step))];
    if (next && next.id !== selectedId) openCandidate(next.id, via);
  }

  const decisionBlocked = !detail
    ? 'Выберите кандидата в очереди'
    : locked
      ? 'Протокол финализирован — решения заблокированы (отмена финализации — Admin/Supervisor).'
      : detail.hypothesis
        ? 'Гипотеза свободного поиска: статус рассмотрения задаётся в блоке «Гипотеза свободного поиска» карточки (принять / отклонить / запросить уточнение).'
        : !DECIDABLE_STATUSES.has(detail.finding_status)
        ? `Технический статус «${label(FINDING_STATUS_LABELS, detail.finding_status)}»: сравнение не состоялось, решение по нарушению не принимается.${detail.no_result ? ` ${detail.no_result.title}: ${detail.no_result.reason}` : ''}`
        : null;

  function choose(key) {
    if (decisionBlocked || saving) return;
    setChoice(key);
    setDecisionError('');
    bump();
  }

  async function saveDecision() {
    if (!choice || !detail || saving) return;
    const needsComment = choice === 'Reject' || choice === 'Clarification Required';
    if (needsComment && comment.trim().length < 3) {
      setDecisionError('Комментарий обязателен при отклонении и запросе уточнения.');
      return;
    }
    bump();
    const m = metrics.current;
    const uiMetrics = { actions: m.actions, elapsed_ms: Math.round(performance.now() - m.openedAt), via: m.via };
    const index = rows.findIndex((r) => r.id === detail.id);
    const nextId = rows.slice(index + 1).find((r) => r.inspector_status === 'PENDING')?.id;
    setSaving(true);
    setDecisionError('');
    try {
      const clarify = choice === 'Clarification Required';
      const kind = CLARIFY_KINDS.find((k) => k.value === clarifyKind);
      await api.evidenceDecision(
        detail.id, choice, choice === 'Reject' ? reasonCode : clarify ? clarifyKind : null,
        clarify ? `${kind.label}: ${comment.trim()}` : comment.trim() || null, uiMetrics,
      );
      await refreshSummary();
      onChanged?.();
      if (nextId) openCandidate(nextId, 'auto');
      else {
        setChoice(null);
        setComment('');
        loadDetail(detail.id);
      }
    } catch (err) {
      setDecisionError(err.message || 'Не удалось сохранить решение');
    } finally {
      setSaving(false);
    }
  }

  async function onHypothesisReviewed(result) {
    setDetail(result.evidence_group);
    await refreshSummary();
    onChanged?.();
  }

  async function onPartsSaved(result) {
    setDetail(result.evidence_group);
    const data = await refreshSummary();
    onChanged?.();
    if (result.group_decision && data) {
      setPartsOpen(false);
      const index = rows.findIndex((r) => r.id === result.evidence_group.id);
      const nextId = rows.slice(index + 1).find((r) => r.inspector_status === 'PENDING' && r.id !== result.evidence_group.id)?.id;
      if (nextId) openCandidate(nextId, 'auto');
    }
  }

  function openSupplement(stage) {
    if (locked) return;
    snapshot.current = new Map((summary?.items || []).map((i) => [i.id, cardSignature(i)]));
    setSupplement({ stage });
  }

  async function onSupplementDone() {
    const before = snapshot.current || new Map();
    setSupplement(null);
    api.listCase10Documents(projectId).then(setDocuments).catch(() => {});
    const data = await refreshSummary();
    const items = data?.items || [];
    const marks = new Map();
    items.forEach((i) => {
      if (!before.has(i.id)) marks.set(i.id, 'new');
      else if (before.get(i.id) !== cardSignature(i)) marks.set(i.id, 'changed');
    });
    const present = new Set(items.map((i) => i.id));
    const gone = [...before.keys()].filter((id) => !present.has(id)).length;
    const changedCount = [...marks.values()].filter((v) => v === 'changed').length;
    setChangedIds(marks);
    setSupplementNote(marks.size || gone
      ? `Дозагрузка завершена: изменились ${changedCount}, добавлены ${marks.size - changedCount}, заменены пересчётом ${gone} карточек (отмечены в очереди). Остальные карточки не тронуты; решения инспектора сохранены.`
      : 'Дозагрузка завершена: ни одна карточка не изменилась, решения инспектора сохранены.');
    onChanged?.();
    if (detail) loadDetail(detail.id);
  }

  function onNoResultAction(action) {
    if (action.kind === 'upload') openSupplement(action.stage);
    else if (action.kind === 'revisions') onOpenSection?.('revisions');
    else if (action.kind === 'edit_evidence') document.getElementById('evidence-editor-anchor')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function submitDraft() {
    if (!draft || !detail) return;
    setDraftBusy(true);
    setDraftError('');
    try {
      let result;
      if (draft.mode === 'add') {
        result = await api.addFragment(detail.id, {
          document_version_id: draft.document_version_id, page: draft.page, bbox_norm: draft.bbox,
          extracted_value: draft.extracted_value || null, role: draft.role, note: draft.note || null, reason: draft.reason,
        });
      } else if (draft.mode === 'refine') {
        const before = draft.original.current || {};
        const body = { reason: draft.reason };
        if (Number(draft.page) !== Number(before.page)) body.page = Number(draft.page);
        if (draft.bboxChanged && draft.bbox) body.bbox_norm = draft.bbox;
        if ((draft.extracted_value || '') !== (before.extracted_value || '')) body.extracted_value = draft.extracted_value;
        if ((draft.role || '') !== (before.role || '')) body.role = draft.role;
        if ((draft.note || '') !== (before.note || '')) body.note = draft.note;
        result = await api.refineFragment(detail.id, draft.key, body);
      } else if (draft.mode === 'remove') {
        result = await api.removeFragment(detail.id, draft.key, draft.reason);
      } else {
        result = await api.restoreFragment(detail.id, draft.key, draft.reason);
      }
      setDetail(result.evidence_group);
      const key = result.edit?.entity_key;
      if (key && draft.mode !== 'remove') setActiveKeys((k) => ({ ...k, [draft.stage]: key }));
      if (draft.mode === 'restore') setShowRemoved(false);
      setDraft(null);
      refreshSummary();
    } catch (err) {
      setDraftError(err.message || 'Не удалось сохранить изменение');
    } finally {
      setDraftBusy(false);
    }
  }

  async function submitBulk({ comment: bulkComment, confirm, reasonCode: bulkReason }) {
    const ids = [...checked];
    await api.bulkDecisions({
      evidence_group_ids: ids, decision: bulk, reason_code: bulk === 'Reject' ? bulkReason : null,
      comment: bulkComment, confirm_bulk_reject: Boolean(confirm),
    });
    setBulk(null);
    setChecked(new Set());
    await refreshSummary();
    onChanged?.();
    if (detail && ids.includes(detail.id)) loadDetail(detail.id);
  }

  useEffect(() => {
    function onKey(event) {
      if (bulk || event.altKey) return;
      if (event.key === 'Escape') {
        if (draft) setDraft(null);
        else if (choice) setChoice(null);
        return;
      }
      if (isTypingTarget(event.target) || event.ctrlKey || event.metaKey) return;
      if (event.code === 'KeyJ' || event.key === 'ArrowDown') {
        event.preventDefault();
        move(1);
      } else if (event.code === 'KeyK' || event.key === 'ArrowUp') {
        event.preventDefault();
        move(-1);
      } else if (DECISION_BY_CODE[event.code]) {
        event.preventDefault();
        metrics.current.via = 'keyboard';
        choose(DECISION_BY_CODE[event.code]);
      } else if (event.key === 'Enter' && choice) {
        event.preventDefault();
        saveDecision();
      } else if (event.code === 'Equal' || event.code === 'NumpadAdd') {
        setZoom((z) => Math.min(ZOOM_MAX, (z === 'auto' ? 2 : z) * 1.5));
      } else if (event.code === 'Minus' || event.code === 'NumpadSubtract') {
        setZoom((z) => Math.max(ZOOM_MIN, (z === 'auto' ? 2 : z) / 1.5));
      } else if (event.code === 'KeyA') {
        setZoom('auto');
      } else if (event.code === 'KeyS' && detail?.parts?.compound && !locked) {
        event.preventDefault();
        setPartsOpen((v) => !v);
      } else if (event.code === 'KeyX' && selectedId) {
        setChecked((prev) => {
          const next = new Set(prev);
          if (next.has(selectedId)) next.delete(selectedId);
          else next.add(selectedId);
          return next;
        });
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }); // re-bound every render on purpose: the handler reads the current state

  const drawSpec = draft && draft.drawing && draft.document_version_id
    ? { ...documentMeta(documents, draft), stage: draft.stage, document_version_id: draft.document_version_id, page: draft.page,
        bbox: draft.bbox, title: draft.mode === 'add' ? 'новый фрагмент' : `уточнение ${draft.key}` }
    : null;
  const position = rows.findIndex((r) => r.id === selectedId);
  const decisions = detail?.decisions || [];

  return (
    <div className="workbench">
      <CandidateQueue
        summary={summary}
        rows={rows}
        filters={filters}
        onFilters={(patch) => setFilters((f) => ({ ...f, ...patch }))}
        selectedId={selectedId}
        onSelect={(id) => openCandidate(id, 'mouse')}
        checked={checked}
        onChecked={setChecked}
        onBulk={setBulk}
        locked={locked}
        changedIds={changedIds}
      />

      <main className="candidate-window">
        {summaryError && <div className="error-banner"><span>{summaryError}</span></div>}
        {!summary && !summaryError && <div className="empty-state"><span className="spinner" /> Загружаю очередь…</div>}
        {summary && !rows.length && <div className="empty-state card">В выбранной очереди нет кандидатов. Смените очередь или фильтр слева.</div>}
        {detailError && <div className="error-banner"><span>{detailError}</span></div>}
        {supplementNote && <div className="info-banner"><span>{supplementNote}</span><button type="button" onClick={() => setSupplementNote('')} aria-label="Закрыть">×</button></div>}

        {detail && (
          <>
            <div className="window-nav">
              <button className="btn small" type="button" onClick={() => move(-1, 'mouse')} disabled={position <= 0}>← <kbd>K</kbd></button>
              <span className="muted small">{position >= 0 ? `${position + 1} из ${rows.length} в очереди` : 'вне текущей очереди'}{loadingDetail ? ' • загрузка…' : ''}</span>
              <button className="btn small" type="button" onClick={() => move(1, 'mouse')} disabled={position < 0 || position >= rows.length - 1}><kbd>J</kbd> →</button>
              <div className="spacer" />
              <span className="muted small hint">Открыть → <kbd>C</kbd>/<kbd>R</kbd>/<kbd>U</kbd> → <kbd>Enter</kbd> • масштаб <kbd>+</kbd>/<kbd>−</kbd>/<kbd>A</kbd> • <kbd>X</kbd> отметить</span>
            </div>

            <div className={`candidate-body${loadingDetail ? ' loading' : ''}`}>
              <EvidenceCardFields detail={detail} locked={locked} onNoResultAction={onNoResultAction} onHypothesisReviewed={onHypothesisReviewed} protocolKey={`${decisions.length}:${detail.parts?.decided || 0}`} />

              <DecisionBar
                disabledReason={decisionBlocked}
                choice={choice}
                reasonCode={reasonCode}
                clarifyKind={clarifyKind}
                onClarifyKind={setClarifyKind}
                compound={Boolean(detail.parts?.compound) && !locked}
                partsDecided={detail.parts?.decided}
                partsTotal={detail.parts?.total}
                partsOpen={partsOpen}
                onSplit={() => setPartsOpen((v) => !v)}
                comment={comment}
                saving={saving}
                error={decisionError}
                actions={actions}
                onChoose={(key) => { metrics.current.via = metrics.current.via === 'keyboard' ? 'keyboard' : 'mouse'; choose(key); }}
                onReason={(value) => { setReasonCode(value); bump(); }}
                onComment={setComment}
                onSave={saveDecision}
                onCancel={() => setChoice(null)}
              />

              {partsOpen && <PartsPanel detail={detail} locked={locked} onSaved={onPartsSaved} onClose={() => setPartsOpen(false)} />}

              <StagePanels
                panels={detail.panels}
                pair={pair}
                onPair={setPair}
                documents={documents}
                section={detail.card?.section}
                locked={locked}
                onSupplement={openSupplement}
                sync={sync}
                onSync={setSync}
                showFrames={showFrames}
                onShowFrames={setShowFrames}
                zoom={zoom}
                onZoom={setZoom}
                activeKeys={activeKeys}
                onActiveKey={(stage, key) => setActiveKeys((k) => ({ ...k, [stage]: key }))}
                draw={drawSpec}
                onDrawn={(bbox) => setDraft((d) => (d ? { ...d, bbox, bboxChanged: true } : d))}
                showRemoved={showRemoved}
              />

              <div id="evidence-editor-anchor" />
              <EvidenceEditor
                detail={detail}
                documents={documents}
                locked={locked}
                draft={draft}
                onDraft={(next) => { setDraft(next); setDraftError(''); }}
                onSubmitDraft={submitDraft}
                busy={draftBusy}
                error={draftError}
                activeKeys={activeKeys}
                onFocus={(stage, key) => setActiveKeys((k) => ({ ...k, [stage]: key }))}
                showRemoved={showRemoved}
                onShowRemoved={setShowRemoved}
              />

              {decisions.length > 0 && (
                <section className="decision-history">
                  <h3>История решений ({decisions.length})</h3>
                  {[...decisions].reverse().map((d) => (
                    <div key={d.id} className="small">
                      {fmtDate(d.created_at)} — <b>{label(INSPECTOR_STATUS_LABELS, INSPECTOR_BY_DECISION[d.decision])}</b>
                      {d.reason_code ? ` • ${d.reason_code}` : ''}{d.comment ? `: «${d.comment}»` : ''}
                      <span className="muted"> • user #{d.user_id}</span>
                    </div>
                  ))}
                </section>
              )}
            </div>
          </>
        )}
      </main>

      {supplement && (
        <SupplementDialog
          projectId={projectId}
          processId={processId}
          stage={supplement.stage}
          section={detail?.card?.section}
          onClose={() => setSupplement(null)}
          onDone={onSupplementDone}
        />
      )}

      {bulk && (
        <BulkDialog
          decision={bulk}
          rows={(summary?.items || []).filter((i) => checked.has(i.id))}
          onCancel={() => setBulk(null)}
          onSubmit={submitBulk}
        />
      )}
    </div>
  );
}
