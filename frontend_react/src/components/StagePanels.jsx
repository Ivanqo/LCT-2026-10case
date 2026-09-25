import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { fetchPageImage, openSourcePage } from '../api.js';
import { APPROVAL_STATUS_LABELS, ROLE_LABELS, STAGE_FULL, STAGE_TITLES, label } from '../labels.js';
import { formatValue } from '../utils.js';

export const ZOOM_MIN = 0.5;
export const ZOOM_MAX = 8;
const AUTO_TARGET = 0.45; // share of the viewport width the active box takes in "auto" zoom

function autoZoom(bbox) {
  if (!bbox) return 1;
  const w = Math.max(0.005, bbox[2] - bbox[0]);
  const h = Math.max(0.005, bbox[3] - bbox[1]);
  return Math.min(6, Math.max(1, Math.min(AUTO_TARGET / w, (AUTO_TARGET * 1.4) / h)));
}

/**
 * The single window's side-by-side view (expert session §15): one panel per stage (ПД / РД / ИД; ИД hidden when
 * the candidate has no ИД evidence), each with file, cipher, revision, approval status, page, the highlighted
 * evidence box, a jump to the original page and zoom. Zoom is shared ("sync"): "auto" fits each panel to its own
 * evidence box, a manual factor applies to all panels at once; every panel re-centers on its box.
 */
export default function StagePanels({ panels, visibleStages, zoom, onZoom, activeKeys, onActiveKey, draw, onDrawn, showRemoved }) {
  const stages = [...visibleStages];
  if (draw && !stages.includes(draw.stage)) stages.push(draw.stage);
  stages.sort((a, b) => ['PD', 'RD', 'ID'].indexOf(a) - ['PD', 'RD', 'ID'].indexOf(b));
  if (!stages.length) {
    return <div className="empty-state card">У кандидата нет доказательных фрагментов с файлом и страницей.</div>;
  }
  return (
    <div className={`stage-panels cols-${stages.length}`}>
      {stages.map((stage) => (
        <StagePanel
          key={stage}
          stage={stage}
          items={(panels?.[stage] || []).filter((item) => item.status !== 'ORPHANED')}
          zoom={zoom}
          onZoom={onZoom}
          activeKey={activeKeys?.[stage]}
          onActiveKey={(key) => onActiveKey(stage, key)}
          draw={draw && draw.stage === stage ? draw : null}
          onDrawn={onDrawn}
          showRemoved={showRemoved}
        />
      ))}
    </div>
  );
}

function StagePanel({ stage, items, zoom, onZoom, activeKey, onActiveKey, draw, onDrawn, showRemoved }) {
  const shown = items.filter((item) => showRemoved || item.status === 'ACTIVE');
  const active = shown.find((item) => item.key === activeKey) || shown.find((item) => item.status === 'ACTIVE') || shown[0] || null;
  const current = draw ? draw : active?.current || null;
  const docId = draw ? draw.document_version_id : current?.document_version_id;
  const page = draw ? draw.page : current?.page;
  const activeBox = draw ? draw.bbox : active?.view_bbox;
  // While drawing, "auto" zoom is frozen at the value it had when draw mode started: re-fitting to every new draft
  // box would re-render and re-center the page under the inspector's mouse.
  const drawZoom = useRef(null);
  if (!draw) drawZoom.current = null;
  else if (drawZoom.current === null) drawZoom.current = autoZoom(draw.bbox);
  const effectiveZoom = zoom === 'auto' ? (draw ? drawZoom.current : autoZoom(activeBox)) : zoom;
  const maxSide = effectiveZoom >= 3 ? 3200 : 2000;
  const centerKey = draw ? `draw:${draw.document_version_id}:${draw.page}` : `${active?.key}:${activeBox?.join(',')}`;

  const [src, setSrc] = useState(null);
  const [error, setError] = useState('');
  const [sourceError, setSourceError] = useState('');
  const viewportRef = useRef(null);
  const canvasRef = useRef(null);
  const [drag, setDrag] = useState(null);

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

  useLayoutEffect(center, [src, effectiveZoom, centerKey]); // eslint-disable-line react-hooks/exhaustive-deps

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

  const overlays = draw
    ? []
    : shown.filter((item) => item.current?.document_version_id === docId && item.current?.page === page && item.view_bbox);
  const multiple = shown.length > 1;

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
              <span>Страница</span><b>{formatValue(page)}</b>
              <span>SHA-256</span><b className="mono" title={current.file_sha256 || ''}>{current.file_sha256 ? `${current.file_sha256.slice(0, 12)}…` : '—'}</b>
            </div>
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
          <button className="btn tiny" type="button" title="Уменьшить (−)" onClick={() => onZoom(Math.max(ZOOM_MIN, +(effectiveZoom / 1.5).toFixed(2)))}>−</button>
          <span className="zoom-value" title="Масштаб общий для всех панелей">{zoom === 'auto' ? `авто ×${effectiveZoom.toFixed(1)}` : `×${effectiveZoom.toFixed(1)}`}</span>
          <button className="btn tiny" type="button" title="Увеличить (+)" onClick={() => onZoom(Math.min(ZOOM_MAX, +(effectiveZoom * 1.5).toFixed(2)))}>+</button>
          <button className="btn tiny" type="button" title="Лист целиком" onClick={() => onZoom(1)}>Лист</button>
          <button className="btn tiny" type="button" title="По фрагменту (A)" onClick={() => onZoom('auto')}>Фрагмент</button>
        </div>
        {docId && page && (
          <button
            className="btn tiny"
            type="button"
            title="Открыть исходную страницу PDF (векторный оригинал, текстовый слой)"
            onClick={() => {
              setSourceError('');
              openSourcePage(docId, page).catch((err) => setSourceError(err.message));
            }}
          >
            Источник ↗
          </button>
        )}
      </div>

      <div className="stage-viewport" ref={viewportRef}>
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
            <img src={src} alt={`${STAGE_TITLES[stage]}: ${current?.file || ''}, стр. ${page}`} draggable={false} onLoad={center} />
            {overlays.map((item) => (
              <BoxOverlay
                key={item.key}
                box={item.view_bbox}
                kind={item.key === active?.key ? 'active' : item.status !== 'ACTIVE' ? 'removed' : item.origin === 'INSPECTOR' ? 'manual' : 'other'}
                title={`${ROLE_LABELS[item.current?.role] || ''}: ${formatValue(item.current?.extracted_value)}`}
                approx={item.view_bbox_quality === 'APPROXIMATE'}
              />
            ))}
            {draw?.bbox && <BoxOverlay box={draw.bbox} kind="draft" title="Новая область" />}
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
          {active.view_bbox_quality === 'APPROXIMATE' && <div className="muted small">Рамка приблизительная: у фрагмента нет координат в системе страницы PDF.</div>}
          {!active.view_bbox && <div className="muted small">У фрагмента нет координат — показана страница целиком.</div>}
        </div>
      ) : null}
    </section>
  );
}

function BoxOverlay({ box, kind, title, approx }) {
  const [x1, y1, x2, y2] = box;
  return (
    <div
      className={`bbox-overlay ${kind}${approx ? ' approx' : ''}`}
      title={title}
      style={{ left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` }}
    />
  );
}
