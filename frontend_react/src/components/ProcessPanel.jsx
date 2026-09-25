import { useCallback, useEffect, useState } from 'react';
import { api, protocolPdfUrl, protocolSubmissionUrl, downloadProtectedFile } from '../api.js';
import Badge from './Badge.jsx';
import Workbench from './Workbench.jsx';
import RevisionsPanel from './RevisionsPanel.jsx';
import CompletenessPanel from './CompletenessPanel.jsx';
import VerificationTimer from './VerificationTimer.jsx';
import { PROCESS_STATUS_LABELS, PROCESS_TONE, label } from '../labels.js';
import { fmtDate } from '../utils.js';

const POLL_STATUSES = new Set(['QUEUED', 'PROCESSING', 'PARSING']);
const LOADED_STATUSES = new Set(['READY', 'VERIFYING', 'COMPLETED', 'FINALIZED']);
const SECTIONS = [
  { key: 'workbench', label: 'Кандидаты и доказательства' },
  { key: 'revisions', label: 'Редакции' },
  { key: 'completeness', label: 'Комплектность' },
];

const SUPERVISOR_ROLES = new Set(['ADMIN', 'SUPERVISOR']);

export default function ProcessPanel({ projectId, processId, onProcessIdChange, me }) {
  const [history, setHistory] = useState([]);
  const [process, setProcess] = useState(null);
  const [protocol, setProtocol] = useState(null);
  const [statusError, setStatusError] = useState('');
  const [pollNonce, setPollNonce] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [showFinalizeConfirm, setShowFinalizeConfirm] = useState(false);
  const [unfinalizing, setUnfinalizing] = useState(false);
  const [showUnfinalizeConfirm, setShowUnfinalizeConfirm] = useState(false);
  const [unfinalizeReason, setUnfinalizeReason] = useState('');
  const [section, setSection] = useState('workbench');
  const [timerKey, setTimerKey] = useState(0);
  const canUnfinalize = Boolean(me?.is_admin || SUPERVISOR_ROLES.has(String(me?.role || '').toUpperCase()));

  const refreshHistory = useCallback(() => {
    if (!projectId) return;
    api.listProcesses(projectId).then(setHistory).catch(() => {});
  }, [projectId]);

  useEffect(() => { refreshHistory(); }, [refreshHistory, processId]);

  // A browser that has never opened this project has no remembered process: open the latest one instead of an
  // empty screen (the inspector should land on the protocol, not on a picker).
  useEffect(() => {
    if (!processId && history.length) onProcessIdChange(history[0].process_id);
  }, [processId, history, onProcessIdChange]);

  const loadProtocol = useCallback(async () => {
    try {
      const data = await api.currentProtocol(projectId, processId);
      setProtocol(data);
    } catch (err) {
      setStatusError(err.message || 'Не удалось загрузить протокол');
    }
  }, [projectId, processId]);

  useEffect(() => {
    if (!processId) {
      setProcess(null);
      setProtocol(null);
      return undefined;
    }
    let active = true;
    let timer = null;
    setStatusError('');

    async function tick() {
      try {
        const data = await api.processStatus(processId);
        if (!active) return;
        setProcess(data);
        setStatusError('');
        if (POLL_STATUSES.has(data.status)) {
          timer = setTimeout(tick, 2000);
        } else if (LOADED_STATUSES.has(data.status)) {
          loadProtocol();
          refreshHistory();
        } else if (data.status === 'FAILED') {
          refreshHistory();
        }
      } catch (err) {
        if (!active) return;
        setStatusError(err.message || 'Не удалось получить статус проверки');
        timer = setTimeout(tick, 4000);
      }
    }
    tick();
    return () => { active = false; clearTimeout(timer); };
  }, [processId, pollNonce, loadProtocol, refreshHistory]);

  async function refreshAfterAction() {
    try {
      const [statusData] = await Promise.all([api.processStatus(processId), loadProtocol()]);
      setProcess(statusData);
      refreshHistory();
    } catch (err) {
      setStatusError('Не удалось обновить состояние проверки: ' + (err.message || err));
    }
  }

  async function startNewProcess() {
    try {
      const created = await api.createProcess(projectId, { run_immediately: true });
      onProcessIdChange(created.process_id);
      refreshHistory();
    } catch (err) {
      setStatusError(err.message || 'Не удалось запустить проверку');
    }
  }

  async function retryProcess() {
    setRetrying(true);
    try {
      await api.runProcess(processId);
      setPollNonce((n) => n + 1);
    } catch (err) {
      setStatusError(err.message || 'Не удалось перезапустить проверку');
    } finally {
      setRetrying(false);
    }
  }

  async function confirmFinalize() {
    if (!protocol) return;
    setFinalizing(true);
    setStatusError('');
    try {
      await api.finalizeProtocol(protocol.id);
      await refreshAfterAction();
      setShowFinalizeConfirm(false);
      setTimerKey((k) => k + 1);
    } catch (err) {
      setStatusError(err.message || 'Не удалось финализировать протокол');
    } finally {
      setFinalizing(false);
    }
  }

  async function confirmUnfinalize() {
    if (!protocol || !unfinalizeReason.trim()) return;
    setUnfinalizing(true);
    setStatusError('');
    try {
      await api.unfinalizeProtocol(protocol.id, unfinalizeReason.trim());
      await refreshAfterAction();
      setShowUnfinalizeConfirm(false);
      setUnfinalizeReason('');
    } catch (err) {
      setStatusError(err.message || 'Не удалось отменить финализацию протокола');
    } finally {
      setUnfinalizing(false);
    }
  }

  if (!projectId) return null;

  const locked = process?.status === 'FINALIZED';
  const completeness = process?.completeness || {};
  const hasCompleteness = Object.keys(completeness).length > 0;
  const sections = protocol?.payload?.sections || {};
  const loaded = process && LOADED_STATUSES.has(process.status);

  return (
    <div>
      <section className="card">
        <div className="card-row">
          <div className="row wrap">
            <span className="muted small">Проверка:</span>
            <select
              className="select"
              value={processId || ''}
              onChange={(e) => onProcessIdChange(e.target.value || null)}
            >
              {!processId && <option value="">— выбрать проверку —</option>}
              {history.map((p) => (
                <option key={p.process_id} value={p.process_id}>
                  {p.process_id.slice(0, 8)} • {label(PROCESS_STATUS_LABELS, p.status)} • {fmtDate(p.created_at)}
                </option>
              ))}
            </select>
          </div>
          <button className="btn small primary" type="button" onClick={startNewProcess}>+ Новая проверка</button>
        </div>
      </section>

      {!processId && (
        <div className="empty-state card">Проверки для этого проекта ещё не запускались. Начните новую проверку или загрузите документы на вкладке «Документы».</div>
      )}

      {processId && process && (
        <section className="card">
          <div className="status-header">
            <div className="main-status">
              <div className="mono small muted">process_id: {process.process_id}</div>
              <Badge tone={PROCESS_TONE[process.status] || 'neutral'}>{label(PROCESS_STATUS_LABELS, process.status)}</Badge>
              {POLL_STATUSES.has(process.status) && (
                <div className="row small muted top-gap"><span className="spinner" /> Опрашиваем статус…</div>
              )}
              {process.job && POLL_STATUSES.has(process.status) && (
                <div className="small muted">Попытка {process.job.attempt_count} из {process.job.max_attempts}</div>
              )}
            </div>
            <div className="status-grid">
              <div className="stat-tile wide-tile"><div className="n obj">{process.object_id || '—'}</div><div className="l">Объект</div></div>
              <div className="stat-tile"><div className="n">{process.upload_scenario || '—'}</div><div className="l">Сценарий загрузки</div></div>
              {protocol && <div className="stat-tile"><div className="n">v{protocol.version}</div><div className="l">Версия протокола</div></div>}
              <div className="stat-tile"><div className="n">{sections.candidates ?? process.pending_candidates ?? 0}</div><div className="l">Кандидатов без решения</div></div>
              <div className="stat-tile"><div className="n">{sections.confirmed_violations || 0}</div><div className="l">Подтверждено</div></div>
              <div className="stat-tile"><div className="n">{sections.negative_verified || 0}</div><div className="l">Нарушения нет</div></div>
            </div>
            {loaded && <VerificationTimer processId={process.process_id} status={process.status} refreshKey={timerKey} />}
          </div>

          {hasCompleteness && (
            <div className="completeness-row top-gap">
              {['PD', 'RD', 'ID'].map((code) => {
                const key = `${code}_UPLOADED`;
                const uploaded = completeness[key] === key;
                return (
                  <Badge key={code} tone={uploaded ? 'ok' : 'warn'}>
                    {code === 'PD' ? 'ПД' : code === 'RD' ? 'РД' : 'ИД'}: {uploaded ? 'загружена' : 'отсутствует'}
                  </Badge>
                );
              })}
            </div>
          )}

          {process.status === 'PENDING' && (
            <div className="top-gap">
              <button className="btn primary" type="button" onClick={retryProcess} disabled={retrying}>
                {retrying ? 'Запуск…' : 'Запустить проверку'}
              </button>
            </div>
          )}

          {process.status === 'FAILED' && (
            <div className="error-banner top-gap">
              <span>Проверка завершилась ошибкой: {process.error || process.job?.error || 'неизвестная ошибка'}</span>
            </div>
          )}
          {process.status === 'FAILED' && (
            <button className="btn top-gap" type="button" onClick={retryProcess} disabled={retrying}>
              {retrying ? 'Перезапуск…' : 'Повторить проверку'}
            </button>
          )}

          {statusError && <div className="error-banner top-gap"><span>{statusError}</span></div>}

          {locked && (
            <div className="finalize-banner top-gap row wrap" style={{ gap: 10, alignItems: 'center' }}>
              <span>Протокол финализирован. Доступен только просмотр.</span>
              {canUnfinalize && (
                <button className="btn small" type="button" onClick={() => setShowUnfinalizeConfirm(true)}>
                  Отменить финализацию
                </button>
              )}
            </div>
          )}

          {protocol && (
            <div className="row wrap top-gap" style={{ gap: 10 }}>
              <button className="btn small" type="button" onClick={() => downloadProtectedFile(protocolPdfUrl(protocol.id), `protocol_${protocol.id}.pdf`)}>
                Скачать протокол (PDF)
              </button>
              <button className="btn small" type="button" onClick={() => downloadProtectedFile(protocolSubmissionUrl(protocol.id), `submission_${protocol.id}.json`)}>
                Экспорт submission (JSON)
              </button>
              <button className="btn small primary" type="button" disabled={locked || finalizing} onClick={() => setShowFinalizeConfirm(true)}>
                {finalizing ? 'Финализация…' : 'Финализировать протокол'}
              </button>
            </div>
          )}
        </section>
      )}

      {showFinalizeConfirm && (
        <div className="modal-overlay" onClick={() => !finalizing && setShowFinalizeConfirm(false)}>
          <div className="modal-box" onClick={(e) => e.stopPropagation()}>
            <h3>Финализировать протокол?</h3>
            <p className="muted">
              {(sections.candidates || 0) > 0
                ? `Есть ${sections.candidates} кандидат(ов) без решения инспектора. Финализация заблокирована, пока по каждому не принято решение (подтверждено, отклонено или переведено в «Требуется уточнение»).`
                : 'После финализации решения инспектора и дозагрузка документов для этой проверки будут заблокированы.'}
            </p>
            <div className="modal-actions">
              <button className="btn" type="button" onClick={() => setShowFinalizeConfirm(false)} disabled={finalizing}>Отмена</button>
              <button
                className="btn primary"
                type="button"
                onClick={confirmFinalize}
                disabled={finalizing || (sections.candidates || 0) > 0}
              >
                {finalizing ? 'Финализация…' : 'Финализировать'}
              </button>
            </div>
          </div>
        </div>
      )}

      {showUnfinalizeConfirm && (
        <div className="modal-overlay" onClick={() => !unfinalizing && setShowUnfinalizeConfirm(false)}>
          <div className="modal-box" onClick={(e) => e.stopPropagation()}>
            <h3>Отменить финализацию протокола?</h3>
            <p className="muted">
              Действие доступно только Admin/Supervisor и фиксируется в аудите. После отмены проверка вернётся в статус
              «Проверено инспектором», решения по находкам снова станут доступны для изменения.
            </p>
            <label className="field">
              <span>Причина (обязательно)</span>
              <textarea
                className="textarea"
                autoFocus
                value={unfinalizeReason}
                onChange={(e) => setUnfinalizeReason(e.target.value)}
                placeholder="Например: обнаружена ошибка в исходных данных, требуется повторная проверка"
              />
            </label>
            <div className="modal-actions">
              <button
                className="btn"
                type="button"
                onClick={() => { setShowUnfinalizeConfirm(false); setUnfinalizeReason(''); }}
                disabled={unfinalizing}
              >
                Отмена
              </button>
              <button
                className="btn primary"
                type="button"
                onClick={confirmUnfinalize}
                disabled={unfinalizing || !unfinalizeReason.trim()}
              >
                {unfinalizing ? 'Отмена финализации…' : 'Отменить финализацию'}
              </button>
            </div>
          </div>
        </div>
      )}

      {protocol && loaded && (
        <>
          <div className="subtabs">
            {SECTIONS.map((item) => (
              <button key={item.key} type="button" className={`subtab${section === item.key ? ' active' : ''}`} onClick={() => setSection(item.key)}>
                {item.label}
              </button>
            ))}
          </div>
          {section === 'workbench' && (
            <Workbench projectId={projectId} processId={process.process_id} locked={locked} onChanged={refreshAfterAction} />
          )}
          {section === 'revisions' && <RevisionsPanel processId={process.process_id} locked={locked} />}
          {section === 'completeness' && <CompletenessPanel processId={process.process_id} />}
        </>
      )}
    </div>
  );
}
