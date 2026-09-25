import { useCallback, useEffect, useRef, useState } from 'react';
import { api, uploadFile } from '../api.js';
import { STAGE_LABELS, APPROVAL_STATUS_LABELS, label } from '../labels.js';
import { fmtDate } from '../utils.js';

const UPLOAD_STATUS_LABELS = {
  processing: 'Обрабатывается…',
  indexed: 'В реестре',
  stalled: 'Задерживается',
  failed: 'Ошибка',
};

export default function DocumentsPanel({ projectId, onProcessStarted }) {
  const [groups, setGroups] = useState([]);
  const [uploads, setUploads] = useState([]);
  const [dragActive, setDragActive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(false);
  const pollRef = useRef(null);
  const fileInputRef = useRef(null);

  const refresh = useCallback(async () => {
    if (!projectId) return [];
    try {
      const data = await api.listCase10Documents(projectId);
      const nextGroups = data.groups || [];
      setGroups(nextGroups);
      setError('');
      return nextGroups;
    } catch (err) {
      setError(err.message || 'Не удалось загрузить список документов');
      return null;
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    refresh();
    return () => clearTimeout(pollRef.current);
  }, [refresh]);

  function watchIndexing(pending, attemptsLeft = 12) {
    clearTimeout(pollRef.current);
    if (!pending.length) return;
    pollRef.current = setTimeout(async () => {
      const nextGroups = await refresh();
      const knownNames = new Set((nextGroups || []).flatMap((g) => g.documents.map((d) => d.filename)));
      const stillPending = [];
      pending.forEach(({ key, name }) => {
        if (knownNames.has(name)) {
          setUploads((prev) => prev.map((u) => (u.key === key ? { ...u, status: 'indexed' } : u)));
        } else {
          stillPending.push({ key, name });
        }
      });
      if (!stillPending.length) return;
      if (attemptsLeft > 1) {
        watchIndexing(stillPending, attemptsLeft - 1);
      } else {
        setUploads((prev) => prev.map((u) => (stillPending.some((p) => p.key === u.key) ? { ...u, status: 'stalled' } : u)));
      }
    }, 2500);
  }

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const pending = [];
    for (const file of files) {
      const uploadKey = `${file.name}-${Date.now()}-${Math.random()}`;
      setUploads((prev) => [...prev, { key: uploadKey, name: file.name, progress: 0, status: 'uploading' }]);
      try {
        await uploadFile(projectId, file, (pct) => {
          setUploads((prev) => prev.map((u) => (u.key === uploadKey ? { ...u, progress: pct } : u)));
        });
        setUploads((prev) => prev.map((u) => (u.key === uploadKey ? { ...u, progress: 100, status: 'processing' } : u)));
        pending.push({ key: uploadKey, name: file.name });
      } catch (err) {
        setUploads((prev) => prev.map((u) => (u.key === uploadKey ? { ...u, status: 'failed', error: err.message } : u)));
      }
    }
    await refresh();
    watchIndexing(pending);
  }

  function onDrop(e) {
    e.preventDefault();
    setDragActive(false);
    handleFiles(e.dataTransfer.files);
  }

  async function deleteDoc(doc) {
    if (!window.confirm(`Удалить документ «${doc.filename}»?`)) return;
    try {
      await api.deleteDocument(doc.id, projectId, doc.source_type || 'rag');
      await refresh();
    } catch (err) {
      setError(err.message || 'Не удалось удалить документ');
    }
  }

  async function startProcess() {
    setStarting(true);
    setError('');
    try {
      const process = await api.createProcess(projectId, { run_immediately: true });
      onProcessStarted(process.process_id);
    } catch (err) {
      setError(err.message || 'Не удалось запустить проверку');
    } finally {
      setStarting(false);
    }
  }

  const totalDocs = groups.reduce((sum, g) => sum + g.documents.length, 0);
  const activeUploads = uploads.filter((u) => u.status === 'uploading' || u.status === 'processing');

  return (
    <div>
      {error && <div className="error-banner"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}

      <section className="card">
        <h2>Загрузка документов</h2>
        <div
          className={`upload-drop${dragActive ? ' active' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragActive(true); }}
          onDragLeave={() => setDragActive(false)}
          onDrop={onDrop}
          onClick={() => fileInputRef.current?.click()}
          role="button"
          tabIndex={0}
        >
          <div>Перетащите файлы ПД / РД / ИД сюда или нажмите, чтобы выбрать (PDF, DOCX, XML, IFC)</div>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            style={{ display: 'none' }}
            onChange={(e) => { handleFiles(e.target.files); e.target.value = ''; }}
          />
        </div>
        <div className="upload-hint">
          Совет: включите в имя файла «ПД», «РД» или «ИД» (project/working/as-built) — так система определяет стадию документа.
        </div>

        {uploads.length > 0 && (
          <div className="top-gap">
            {uploads.map((u) => (
              <div className="upload-row" key={u.key}>
                <div style={{ flex: 1 }}>
                  <div>{u.name}</div>
                  <div className="progress-track">
                    <div
                      className="progress-bar"
                      style={{
                        width: `${u.progress}%`,
                        background: u.status === 'failed' || u.status === 'stalled' ? 'var(--bad)' : u.status === 'indexed' ? 'var(--ok)' : undefined,
                      }}
                    />
                  </div>
                  {u.status === 'failed' && <div className="small" style={{ color: 'var(--bad)' }}>{u.error}</div>}
                  {u.status === 'stalled' && (
                    <div className="small" style={{ color: 'var(--bad)' }}>
                      Файл загружен, но пока не появился в реестре документов. Проверьте сервис обработки документов или попробуйте загрузить файл ещё раз.
                    </div>
                  )}
                </div>
                <span className="muted small">
                  {u.status === 'uploading' ? `${u.progress}%` : UPLOAD_STATUS_LABELS[u.status] || u.status}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-row">
          <h2 style={{ margin: 0 }}>Документы проекта ({totalDocs})</h2>
          <button className="btn small" type="button" onClick={refresh}>Обновить</button>
        </div>

        {loading && <div className="muted top-gap">Загрузка…</div>}
        {!loading && totalDocs === 0 && (
          <div className="empty-state">Документы ещё не загружены. Загрузите ПД, РД и ИД, чтобы начать проверку.</div>
        )}
        {!loading && groups.map((group) => (
          <div className="doc-stage-group top-gap" key={group.stage}>
            <div className="doc-stage-title">
              <span className="pill">{group.stage_label || label(STAGE_LABELS, group.stage)}</span>
              <span className="muted small">{group.documents.length} файл(ов)</span>
            </div>
            {group.documents.map((doc) => (
              <div className="doc-row" key={doc.id}>
                <div>
                  <div className="name">{doc.filename}</div>
                  <div className="doc-meta">
                    {label(APPROVAL_STATUS_LABELS, doc.approval_status)}
                    {doc.revision ? ` • ред. ${doc.revision}` : ''}
                    {' • '}
                    {fmtDate(doc.uploaded_at || doc.created_at)}
                    {doc.is_current_approved ? ' • действующая утверждённая версия' : ''}
                  </div>
                </div>
                <button className="btn small danger" type="button" onClick={() => deleteDoc(doc)}>Удалить</button>
              </div>
            ))}
          </div>
        ))}
      </section>

      <section className="card">
        <div className="card-row">
          <div>
            <h2 style={{ margin: 0 }}>Запуск проверки</h2>
            <div className="muted small top-gap">
              Проверка сопоставит загруженные ПД/РД/ИД с матрицей параметров и создаст карточки доказательств.
            </div>
          </div>
          <button
            className="btn primary"
            type="button"
            disabled={starting || totalDocs === 0 || activeUploads.length > 0}
            onClick={startProcess}
          >
            {starting ? 'Запуск…' : 'Начать проверку'}
          </button>
        </div>
        {totalDocs === 0 && <div className="muted small top-gap">Сначала загрузите хотя бы один документ.</div>}
      </section>
    </div>
  );
}
