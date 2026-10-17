import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api, fetchPageImage, openSourcePage } from '../api.js';
import { APPROVAL_STATUS_LABELS, ROLE_LABELS, STAGE_FULL, STAGE_GENITIVE, STAGE_TITLES, label } from '../labels.js';
import { formatValue } from '../utils.js';

export const ZOOM_MIN = 0.5;
export const ZOOM_MAX = 8;
const AUTO_TARGET = 0.45; // share of the viewport width the active box takes in "auto" zoom
const STAGE_BY_DOC_STAGE = { project: 'PD', working: 'RD', as_built: 'ID' };

/** The pair the inspector compares (expert checklist 2): documents are only shown side by side when chosen. */
export const PAIRS = [
  { key: 'PD_RD_ID', label: 'ПД / РД и ИД', stages: ['PD', 'RD', 'ID'] },
  { key: 'PD_RD', label: 'ПД / РД', stages: ['PD', 'RD'] },
  { key: 'PD_ID', label: 'ПД / ИД', stages: ['PD', 'ID'] },
];

function autoZoom(bbox) {
  if (!bbox) return 1;
  const w = Math.max(0.005, bbox[2] - bbox[0]);
  const h = Math.max(0.005, bbox[3] - bbox[1]);
  return Math.min(6, Math.max(1, Math.min(AUTO_TARGET / w, (AUTO_TARGET * 1.4) / h)));
}

const geometryCache = new Map();
function pageCount(docId, page) {
  const key = `${docId}`;
  if (!geometryCache.has(key)) {
    const promise = api.pageGeometry(docId, page).then((g) => g.page_count || null);
    promise.catch(() => geometryCache.delete(key));
    geometryCache.set(key, promise);
  }
  return geometryCache.get(key);
}

function documentCount(documents, stage) {
  let count = 0;
  for (const group of documents?.groups || []) {
    for (const doc of group.documents || []) {
      if (doc.dataset_stage === stage || STAGE_BY_DOC_STAGE[doc.doc_stage || group.stage] === stage) count += 1;
    }
  }
  return count;
}

/**
 * The single window's side-by-side view (expert session §15, checklist 2/4/5): the chosen pair of stages (ПД/РД,
 * ПД/ИД, ПД/РД и ИД) as panels; a stage without documents is shown as a placeholder with "Дозагрузить" instead of
 * silently disappearing. Panels can be driven independently or in sync (zoom and scroll), switch pages, go full
 * screen, hide the frames, and save a snapshot of the fragment (the full sheet opens through "Источник ↗").
 */
export default function StagePanels({
  panels, pair, onPair, documents, section, locked, onSupplement, zoom, onZoom, sync, onSync, showFrames, onShowFrames,
  activeKeys, onActiveKey, draw, onDrawn, showRemoved,
}) {
  const stages = [...(PAIRS.find((p) => p.key === pair) || PAIRS[0]).stages];
  if (draw && !stages.includes(draw.stage)) stages.push(draw.stage);
  stages.sort((a, b) => ['PD', 'RD', 'ID'].indexOf(a) - ['PD', 'RD', 'ID'].indexOf(b));
  const wrapRef = useRef(null);
  const viewports = useRef({});
  const [full, setFull] = useState(false);
  const [pseudoFull, setPseudoFull] = useState(false);

  useEffect(() => {
    const onChange = () => setFull(document.fullscreenElement === wrapRef.current);
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  async function toggleFullscreen() {
    if (document.fullscreenElement || pseudoFull) {
      setPseudoFull(false);
      if (document.fullscreenElement) await document.exitFullscreen().catch(() => {});
      return;
    }
    try {
      await wrapRef.current.requestFullscreen();
    } catch {
      setPseudoFull(true); // the browser refused native full screen: a fixed overlay does the same job
    }
  }

  function syncScroll(fromStage, el) {
    const ratioX = el.scrollWidth > el.clientWidth ? el.scrollLeft / (el.scrollWidth - el.clientWidth) : 0;
    const ratioY = el.scrollHeight > el.clientHeight ? el.scrollTop / (el.scrollHeight - el.clientHeight) : 0;
    Object.entries(viewports.current).forEach(([stage, other]) => {
      if (stage === fromStage || !other) return;
      other.scrollLeft = ratioX * Math.max(0, other.scrollWidth - other.clientWidth);
      other.scrollTop = ratioY * Math.max(0, other.scrollHeight - other.clientHeight);
    });
  }

  const isFull = full || pseudoFull;
  return (
    <div className={`panels-wrap${isFull ? ' is-full' : ''}`} ref={wrapRef}>
      <div className="panels-toolbar" role="toolbar" aria-label="Режим просмотра листов">
        <div className="seg" role="group" aria-label="Пара документов">
          {PAIRS.map((p) => (
            <button key={p.key} type="button" className={`seg-btn${pair === p.key ? ' active' : ''}`} onClick={() => onPair(p.key)} title="Какие стадии сравниваются в панелях">
              {p.label}
            </button>
          ))}
        </div>
        <button type="button" className={`btn tiny${sync ? ' toggled' : ''}`} onClick={() => onSync(!sync)}
          title="Синхронно: масштаб и прокрутка всех панелей общие. Независимо: каждая панель управляется отдельно.">
          {sync ? 'Панели синхронны' : 'Панели независимы'}
        </button>
        <button type="button" className={`btn tiny${showFrames ? '' : ' toggled'}`} onClick={() => onShowFrames(!showFrames)}
          title="Скрыть или показать красные рамки и метки на листах (привязка рамок сохраняется)">
          {showFrames ? 'Скрыть рамки' : 'Показать рамки'}
        </button>
        <div className="spacer" />
        <button type="button" className="btn tiny" onClick={toggleFullscreen} title="Панели во весь экран">
          {isFull ? 'Выйти из полного экрана' : 'Во весь экран'}
        </button>
      </div>
      <div className={`stage-panels cols-${stages.length}${stages.some((s) => !(panels?.[s] || []).some((i) => i.status !== 'ORPHANED') && !(draw && draw.stage === s)) ? ' has-missing' : ''}`}>
        {stages.map((stage) => {
          const items = (panels?.[stage] || []).filter((item) => item.status !== 'ORPHANED');
          if (!items.length && !(draw && draw.stage === stage)) {
            return <MissingStagePanel key={stage} stage={stage} documents={documents} section={section} locked={locked} onSupplement={onSupplement} />;
          }
          return (
            <StagePanel
              key={stage}
              stage={stage}
              items={items}
              zoom={zoom}
              onZoom={onZoom}
              sync={sync}
              activeKey={activeKeys?.[stage]}
              onActiveKey={(key) => onActiveKey(stage, key)}
              draw={draw && draw.stage === stage ? draw : null}
              onDrawn={onDrawn}
              showRemoved={showRemoved}
              showFrames={showFrames}
              layoutKey={isFull}
              registerViewport={(el) => { viewports.current[stage] = el; }}
              onUserScroll={(el) => { if (sync) syncScroll(stage, el); }}
            />
          );
        })}
      </div>
    </div>
  );
}

function MissingStagePanel({ stage, documents, section, locked, onSupplement }) {
  const uploaded = documentCount(documents, stage);
  const title = STAGE_TITLES[stage];
  return (
    <section className={`stage-panel stage-${stage} missing`} aria-label={`Панель ${title}: нет доказательств`}>
      <header className="stage-head">
        <div className="stage-title">
          <span className={`stage-chip stage-${stage}`}>{title}</span>
          <span className="muted small">{STAGE_FULL[stage]}</span>
        </div>
      </header>
      <div className="missing-body">
        <div className="missing-title">{uploaded ? `${title}: в записи нет доказательств этой стадии` : `${title}: документы не загружены`}</div>
        <p className="muted small">
          {uploaded
            ? `Документов ${title} в проекте: ${uploaded}, но для этой записи значения и страницы не найдены. Проверьте, что нужный документ относится к разделу «${section || '—'}», дозагрузите его или укажите фрагмент вручную («Добавить фрагмент»).`
            : `Сравнение невозможно без ${STAGE_GENITIVE[stage]}. Рекомендуем загрузить документы стадии ${title}${section ? ` по разделу «${section}»` : ''}; после загрузки пересчитываются только затронутые параметры, решения сохраняются.`}
        </p>
        <button type="button" className="btn primary" onClick={() => onSupplement(stage)} disabled={locked}
          title={locked ? 'Протокол финализирован — дозагрузка заблокирована' : `Загрузить документы ${title}`}>
          Дозагрузить
        </button>
        {locked && <div className="muted small">Протокол финализирован: дозагрузка заблокирована.</div>}
      </div>
    </section>
  );
}

function StagePanel({ stage, items, zoom, onZoom, sync, activeKey, onActiveKey, draw, onDrawn, showRemoved, showFrames, layoutKey, registerViewport, onUserScroll }) {
  const shown = items.filter((item) => showRemoved || item.status === 'ACTIVE');
  const active = shown.find((item) => item.key === activeKey) || shown.find((item) => item.status === 'ACTIVE') || shown[0] || null;
  const current = draw ? draw : active?.current || null;
  const docId = draw ? draw.document_version_id : current?.document_version_id;
  const fragmentPage = draw ? draw.page : current?.page;
  const [pageOverride, setPageOverride] = useState(null);
  const page = pageOverride ?? fragmentPage;
  const onFragmentPage = page === fragmentPage;
  const activeBox = onFragmentPage ? (draw ? draw.bbox : active?.view_bbox) : null;
  const [localZoom, setLocalZoom] = useState('auto');
  const panelZoom = sync ? zoom : localZoom;
  const setPanelZoom = (value) => (sync ? onZoom(value) : setLocalZoom(value));
  // While drawing, "auto" zoom is frozen at the value it had when draw mode started: re-fitting to every new draft
  // box would re-render and re-center the page under the inspector's mouse.
  const drawZoom = useRef(null);
  if (!draw) drawZoom.current = null;
  else if (drawZoom.current === null) drawZoom.current = autoZoom(draw.bbox);
  const effectiveZoom = panelZoom === 'auto' ? (draw ? drawZoom.current : autoZoom(activeBox || active?.view_bbox)) : panelZoom;
  const maxSide = effectiveZoom >= 3 ? 3200 : 2000;
  const centerKey = draw ? `draw:${draw.document_version_id}:${draw.page}` : `${active?.key}:${activeBox?.join(',')}:${page}`;

  const [src, setSrc] = useState(null);
  const [error, setError] = useState('');
  const [sourceError, setSourceError] = useState('');
  const [pages, setPages] = useState(null);
  const viewportRef = useRef(null);
  const canvasRef = useRef(null);
  const imgRef = useRef(null);
  const userScroll = useRef(false);
  const [drag, setDrag] = useState(null);

  useEffect(() => { setPageOverride(null); }, [active?.key, docId, fragmentPage]);

  useEffect(() => {
    if (!docId || !fragmentPage) {
      setPages(null);
      return undefined;
    }
    let cancelled = false;
    pageCount(docId, fragmentPage).then((n) => !cancelled && setPages(n)).catch(() => !cancelled && setPages(null));
    return () => { cancelled = true; };
  }, [docId, fragmentPage]);

  useEffect(() => {
    if (!docId || !page) {
      setSrc(null);
      return undefined;
    }
    let cancelled = false;
    setSrc(null);
    setError('');
    fetchPageImage(docId, page, maxSide)
      .then((url) => !cancelled && setSrc(url))
      .catch((err) => !cancelled && setError(err.message || 'Не удалось загрузить страницу'));
    return () => {
      cancelled = true;
    };
  }, [docId, page, maxSide]);

  function center() {
    const viewport = viewportRef.current;
    const canvas = canvasRef.current;
    if (!viewport || !canvas) return;
    if (!activeBox) {
      viewport.scrollTo({ left: 0, top: 0 });
      return;
    }
    const cx = ((activeBox[0] + activeBox[2]) / 2) * canvas.offsetWidth;
    const cy = ((activeBox[1] + activeBox[3]) / 2) * canvas.offsetHeight;
    viewport.scrollTo({ left: Math.max(0, cx - viewport.clientWidth / 2), top: Math.max(0, cy - viewport.clientHeight / 2) });
  }

  useLayoutEffect(center, [src, effectiveZoom, centerKey, layoutKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    registerViewport(viewportRef.current);
    return () => registerViewport(null);
  }); // the viewport node is stable; re-registering every render is cheap and survives remounts

  useEffect(() => {
    const release = () => setTimeout(() => { userScroll.current = false; }, 150);
    window.addEventListener('pointerup', release);
    return () => window.removeEventListener('pointerup', release);
  }, []);

  function markUser(durationMs) {
    userScroll.current = true;
    if (durationMs) setTimeout(() => { userScroll.current = false; }, durationMs);
  }

  function toNorm(event) {
    const rect = canvasRef.current.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const y = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height));
    return [x, y];
  }

  function onMouseDown(event) {
    if (!draw || event.button !== 0) return;
    event.preventDefault();
    const [x, y] = toNorm(event);
    setDrag({ x0: x, y0: y, x1: x, y1: y });
  }

  function onMouseMove(event) {
    if (!drag) return;
    const [x, y] = toNorm(event);
    setDrag((d) => ({ ...d, x1: x, y1: y }));
  }

  function onMouseUp() {
    if (!drag) return;
    const box = [Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1), Math.max(drag.x0, drag.x1), Math.max(drag.y0, drag.y1)];
    setDrag(null);
    if (box[2] - box[0] > 0.002 && box[3] - box[1] > 0.002) {
      onDrawn(box.map((v) => Math.round(v * 100000) / 100000));
    }
  }

  function savePageSnapshot() {
    const img = imgRef.current;
    const box = active?.view_bbox;
    if (!img || !img.naturalWidth || !box || !onFragmentPage) return;
    const pad = 0.015;
    const x0 = Math.max(0, box[0] - pad);
    const y0 = Math.max(0, box[1] - pad);
    const x1 = Math.min(1, box[2] + pad);
    const y1 = Math.min(1, box[3] + pad);
    const sx = x0 * img.naturalWidth;
    const sy = y0 * img.naturalHeight;
    const sw = Math.max(8, (x1 - x0) * img.naturalWidth);
    const sh = Math.max(8, (y1 - y0) * img.naturalHeight);
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(sw);
    canvas.height = Math.round(sh);
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
    ctx.strokeStyle = '#ef1a1a';
    ctx.lineWidth = Math.max(2, sw / 250);
    ctx.strokeRect((box[0] - x0) * img.naturalWidth, (box[1] - y0) * img.naturalHeight, (box[2] - box[0]) * img.naturalWidth, (box[3] - box[1]) * img.naturalHeight);
    canvas.toBlob((blob) => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `fragment_${stage}_${current?.file_id || 'doc'}_p${page}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, 'image/png');
  }

  const overlays = draw || !showFrames
    ? []
    : shown.filter((item) => item.current?.document_version_id === docId && item.current?.page === page && item.view_bbox);
  const multiple = shown.length > 1;
  const sheet = active?.sheet;
  const stageInfo = active?.stage_info;
  const canPage = Boolean(docId && pages && pages > 1);

  return (
    <section className={`stage-panel stage-${stage}${draw ? ' drawing' : ''}`} aria-label={`Панель ${STAGE_TITLES[stage]}`}>
      <header className="stage-head">
        <div className="stage-title">
          <span className={`stage-chip stage-${stage}`}>{STAGE_TITLES[stage]}</span>
          <span className="muted small">{STAGE_FULL[stage]}</span>
          {draw && <span className="pill warn-pill">режим выделения</span>}
        </div>
        {current ? (
          <div className="stage-meta">
            <div className="stage-file" title={current.file || ''}>{current.file || current.file_id || 'Файл не определён'}</div>
            <div className="meta-grid">
              <span>ID файла</span><b className="mono">{formatValue(current.file_id)}</b>
              <span>Шифр</span><b className="mono">{formatValue(current.document_code)}</b>
              <span>Редакция</span><b>{formatValue(current.revision)}</b>
              <span>Статус</span><b>{label(APPROVAL_STATUS_LABELS, current.approval_status, 'не указан')}</b>
              <span>Страница</span><b>{formatValue(fragmentPage)}{pages ? ` из ${pages}` : ''}</b>
              <span>Лист</span>
              <b title={sheet ? `${sheet.basis}${sheet.cipher ? `; шифр ${sheet.cipher}` : ''}` : 'В основной надписи страницы обозначение листа не найдено'}>
                {sheet?.label || '—'}
              </b>
              <span>SHA-256</span><b className="mono" title={current.file_sha256 || ''}>{current.file_sha256 ? `${current.file_sha256.slice(0, 12)}…` : '—'}</b>
              <span>Стадия по</span>
              <b title={(stageInfo?.sources || []).map((s) => `${s.basis}: ${s.stage_label || STAGE_TITLES[s.stage] || 'не определена'}`).join('\n')}>
                {(stageInfo?.sources || []).map((s) => `${s.basis.split(' ')[0]} ${s.stage_label || STAGE_TITLES[s.stage] || '—'}`).join(' · ') || '—'}
              </b>
            </div>
            {stageInfo?.warning && <div className="stage-warning" role="alert">⚠ Источник стадии расходится: {stageInfo.warning}. Подтвердите стадию по реестру файлов.</div>}
          </div>
        ) : (
          <div className="muted small">Нет доказательств этой стадии.</div>
        )}
      </header>

      {multiple && !draw && (
        <div className="fragment-tabs">
          {shown.map((item, index) => (
            <button
              key={item.key}
              type="button"
              className={`frag-tab${item.key === active?.key ? ' active' : ''}${item.status !== 'ACTIVE' ? ' removed' : ''}${item.origin === 'INSPECTOR' ? ' manual' : ''}`}
              onClick={() => onActiveKey(item.key)}
              title={`${ROLE_LABELS[item.current?.role] || item.current?.role || ''} • стр. ${item.current?.page}`}
            >
              {index + 1}. {ROLE_LABELS[item.current?.role] || 'фрагмент'} • с.{item.current?.page}
            </button>
          ))}
        </div>
      )}

      <div className="stage-toolbar">
        <div className="row" style={{ gap: 4 }}>
          <button className="btn tiny" type="button" title="Уменьшить (−)" onClick={() => setPanelZoom(Math.max(ZOOM_MIN, +(effectiveZoom / 1.5).toFixed(2)))}>−</button>
          <span className="zoom-value" title={sync ? 'Масштаб общий для всех панелей' : 'Масштаб этой панели'}>{panelZoom === 'auto' ? `авто ×${effectiveZoom.toFixed(1)}` : `×${effectiveZoom.toFixed(1)}`}</span>
          <button className="btn tiny" type="button" title="Увеличить (+)" onClick={() => setPanelZoom(Math.min(ZOOM_MAX, +(effectiveZoom * 1.5).toFixed(2)))}>+</button>
          <button className="btn tiny" type="button" title="Лист целиком" onClick={() => setPanelZoom(1)}>Лист</button>
          <button className="btn tiny" type="button" title="Вернуться к исходной области фрагмента (A)" onClick={() => { setPageOverride(null); setPanelZoom('auto'); center(); }}>Фрагмент</button>
        </div>
        {canPage && !draw && (
          <div className="row page-switch" style={{ gap: 4 }} title="Переключение страниц документа">
            <button className="btn tiny" type="button" onClick={() => setPageOverride(Math.max(1, page - 1))} disabled={page <= 1} aria-label="Предыдущая страница">‹</button>
            <input
              className="input thin page-input"
              type="number"
              min={1}
              max={pages}
              value={page}
              onChange={(e) => {
                const n = Math.min(pages, Math.max(1, Number(e.target.value) || 1));
                setPageOverride(n === fragmentPage ? null : n);
              }}
              aria-label="Номер страницы"
            />
            <span className="muted small">/ {pages}</span>
            <button className="btn tiny" type="button" onClick={() => setPageOverride(Math.min(pages, page + 1))} disabled={page >= pages} aria-label="Следующая страница">›</button>
            {!onFragmentPage && <button className="btn tiny" type="button" onClick={() => setPageOverride(null)} title={`К странице фрагмента (${fragmentPage})`}>к фрагменту</button>}
          </div>
        )}
        <div className="row" style={{ gap: 4 }}>
          {!draw && active?.view_bbox && (
            <button className="btn tiny" type="button" onClick={savePageSnapshot} disabled={!src || !onFragmentPage} title="Сохранить снимок фрагмента с рамкой (PNG)">
              Снимок фрагмента
            </button>
          )}
          {docId && page && (
            <button
              className="btn tiny"
              type="button"
              title="Открыть полный лист: исходная страница PDF (векторный оригинал, текстовый слой)"
              onClick={() => {
                setSourceError('');
                openSourcePage(docId, page).catch((err) => setSourceError(err.message));
              }}
            >
              Источник ↗
            </button>
          )}
        </div>
      </div>

      <div
        className="stage-viewport"
        ref={viewportRef}
        onWheel={() => markUser(300)}
        onPointerDown={() => markUser()}
        onScroll={(e) => { if (userScroll.current) onUserScroll(e.currentTarget); }}
      >
        {!docId && <div className="empty-state">—</div>}
        {docId && !src && !error && (
          <div className="row" style={{ padding: 30, justifyContent: 'center' }}><span className="spinner" /> <span className="muted">Загружаю страницу…</span></div>
        )}
        {error && <div className="error-banner" style={{ margin: 10 }}><span>{error}</span></div>}
        {src && (
          <div
            className="page-canvas"
            ref={canvasRef}
            style={{ width: `${effectiveZoom * 100}%` }}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={onMouseUp}
            onMouseLeave={onMouseUp}
          >
            <img ref={imgRef} src={src} alt={`${STAGE_TITLES[stage]}: ${current?.file || ''}, стр. ${page}`} draggable={false} onLoad={center} />
            {overlays.map((item) => (
              <BoxOverlay
                key={item.key}
                box={item.view_bbox}
                kind={item.key === active?.key ? 'active' : item.status !== 'ACTIVE' ? 'removed' : item.origin === 'INSPECTOR' ? 'manual' : 'other'}
                title={`${ROLE_LABELS[item.current?.role] || ''}: ${formatValue(item.current?.extracted_value)}`}
                text={`${formatValue(item.current?.extracted_value)} · ${STAGE_TITLES[stage]}`}
                approx={item.view_bbox_quality === 'APPROXIMATE'}
              />
            ))}
            {draw?.bbox && <BoxOverlay box={draw.bbox} kind="draft" title="Новая область" text={`новая область · ${STAGE_TITLES[stage]}`} />}
            {drag && (
              <BoxOverlay
                box={[Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1), Math.max(drag.x0, drag.x1), Math.max(drag.y0, drag.y1)]}
                kind="draft"
              />
            )}
          </div>
        )}
      </div>

      {sourceError && <div className="error-banner" style={{ margin: '6px 0 0' }}><span>{sourceError}</span></div>}

      {draw ? (
        <div className="stage-caption warn">Выделите мышью область на странице — {draw.title || 'новый фрагмент'}.</div>
      ) : active ? (
        <div className="stage-caption">
          <span className="muted small">{ROLE_LABELS[active.current?.role] || 'фрагмент'}{active.origin === 'INSPECTOR' ? ' • добавлен инспектором' : active.edited ? ` • уточнён (v${active.version})` : ' • машинный вывод'}{active.status === 'REMOVED' ? ' • исключён' : ''}</span>
          <div className="caption-value">{formatValue(active.current?.extracted_value)}</div>
          {active.current?.context && <div className="fragment-context">«{String(active.current.context).slice(0, 180)}»</div>}
          {!showFrames && <div className="muted small">Рамки скрыты (кнопка «Показать рамки» вверху).</div>}
          {!onFragmentPage && <div className="muted small">Показана страница {page}; фрагмент находится на странице {fragmentPage}.</div>}
          {active.view_bbox_quality === 'APPROXIMATE' && <div className="muted small">Рамка приблизительная: у фрагмента нет координат в системе страницы PDF.</div>}
          {!active.view_bbox && <div className="muted small">У фрагмента нет координат — показана страница целиком.</div>}
        </div>
      ) : null}
    </section>
  );
}

function BoxOverlay({ box, kind, title, text, approx }) {
  const [x1, y1, x2, y2] = box;
  return (
    <div
      className={`bbox-overlay ${kind}${approx ? ' approx' : ''}`}
      title={title}
      style={{ left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` }}
    >
      {text && <span className={`bbox-label${y1 < 0.04 ? ' below' : ''}`}>{String(text).slice(0, 48)}</span>}
    </div>
  );
}
