import { useRef, useState } from 'react';
import { api, uploadFile } from '../api.js';
import { STAGE_GENITIVE, STAGE_TITLES } from '../labels.js';

const RUNNING = new Set(['QUEUED', 'PROCESSING', 'PARSING']);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * "Дозагрузить" (ТЗ 9.3, checklist 10): upload the missing documents from the card itself. The server attaches them
 * to the open process and recomputes only the affected parameters; the inspector's decisions are kept. The caller
 * receives the moment the recomputation settles and highlights the cards that changed.
 */
export default function SupplementDialog({ projectId, processId, stage, section, onClose, onDone }) {
  const [files, setFiles] = useState([]);
  const [lines, setLines] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const input = useRef(null);
  const title = STAGE_TITLES[stage] || 'документы';

  const log = (text) => setLines((l) => [...l, text]);

  async function run() {
    setBusy(true);
    setError('');
    setLines([]);
    try {
      const names = [];
      for (const file of files) {
        log(`Загружаю ${file.name}…`);
        await uploadFile(projectId, file, undefined, processId); // attached to the check under review
        names.push(file.name);
        log(`${file.name}: принят сервером`);
      }
      log('Жду появления файлов в реестре документов…');
      let listed = false;
      for (let i = 0; i < 16 && !listed; i += 1) {
        await sleep(2500);
        const data = await api.listCase10Documents(projectId);
        const known = new Set((data.groups || []).flatMap((g) => g.documents.map((d) => d.filename)));
        listed = names.every((n) => known.has(n));
      }
      if (!listed) throw new Error('Файлы загружены, но пока не появились в реестре: сервис обработки документов не ответил. Повторите позже.');
      log('Файлы в реестре. Жду пересчёта затронутых параметров…');
      let settled = false;
      for (let i = 0; i < 30 && !settled; i += 1) {
        await sleep(2000);
        const status = await api.processStatus(processId);
        settled = !RUNNING.has(status.status);
      }
      log(settled ? 'Пересчёт завершён.' : 'Пересчёт ещё идёт: карточки обновятся при следующем открытии.');
      onDone({ names, settled });
    } catch (err) {
      setError(err.message || 'Не удалось дозагрузить документы');
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={() => !busy && onClose()}>
      <div className="modal-box wide" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Дозагрузить документы">
        <h3>Дозагрузить документы {title}</h3>
        <p className="muted small">
          {stage ? `Рекомендуем загрузить документы стадии ${title} (комплект ${STAGE_GENITIVE[stage]})${section ? ` по разделу «${section}»` : ''}.` : 'Загрузите недостающие документы.'}{' '}
          Включите в имя файла «ПД», «РД» или «ИД» — так система определяет стадию. Решения инспектора сохраняются, пересчитываются только затронутые параметры,
          изменившиеся карточки будут отмечены в очереди.
        </p>
        <input ref={input} type="file" multiple accept=".pdf,.docx,.xml,.ifc" onChange={(e) => setFiles(Array.from(e.target.files || []))} disabled={busy} aria-label="Файлы для дозагрузки" />
        {files.length > 0 && <ul className="small">{files.map((f) => <li key={f.name}>{f.name} <span className="muted">({Math.round(f.size / 1024)} КБ)</span></li>)}</ul>}
        {lines.length > 0 && <div className="supplement-log small">{lines.map((l, i) => <div key={i}>{l}</div>)}</div>}
        {error && <div className="error-banner"><span>{error}</span></div>}
        <div className="modal-actions">
          <button className="btn" type="button" onClick={onClose} disabled={busy}>Закрыть</button>
          <button className="btn primary" type="button" onClick={run} disabled={busy || !files.length}>{busy ? 'Выполняется…' : 'Загрузить и пересчитать'}</button>
        </div>
      </div>
    </div>
  );
}
