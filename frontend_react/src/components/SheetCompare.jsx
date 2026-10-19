import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, fetchPageImage, fetchSheetOverlay } from '../api.js';

const KIND_OPTIONS = [
  { key: 'all', label: 'Все пары' },
  { key: 'stage', label: 'ПД ↔ РД' },
  { key: 'revision', label: 'РД ↔ РД (редакции)' },
];
const KIND_LABEL = { stage: 'ПД ↔ РД', revision: 'РД: редакции' };
const VIEW_OPTIONS = [
  { key: 'panels', label: 'Две панели' },
  { key: 'overlay', label: 'Наложение различий' },
];
const LAYOUT_TEXT = {
  same: 'Листы скомпонованы одинаково: различия локализованы рамками.',
  partial: 'Листы схожи частично: рамки показывают только крупные различия.',
  different: 'Листы скомпонованы по-разному (другой масштаб или состав листа): различия не локализуются, рамки не показываются.',
};
const REASON_TEXT = {
  format_mismatch: 'Разные форматы или ориентация листов: растровое сравнение не выполняется.',
  no_strokes: 'На одном из листов нет штрихов чертежа (скан без вектора или пустой лист).',
};
const COMPONENT_LABELS = { title: 'название', levels: 'отметка', rooms: 'помещения', labels: 'метки', cipher: 'шифр', kind: 'тип листа', format: 'формат' };

const pct = (value) => (typeof value === 'number' ? `${(value * 100).toFixed(1)} %` : '—');
const conf = (value) => (typeof value === 'number' ? value.toFixed(2) : '—');
const pairKey = (p) => `${p.left.document_id}:${p.left.page}|${p.right.document_id}:${p.right.page}`;

/** Boxes of the difference regions over a page image: PDF points -> percentages of the visible page. */
function RegionBoxes({ regions, side, size, activeId, onPick }) {
  if (!regions?.length || !size) return null;
  return regions.map((region) => {
    const [x0, y0, x1, y1] = side === 'left' ? region.bbox_left : region.bbox_right;
    return (
      <button
        key={region.id}
        type="button"
        className={`sheet-region${activeId === region.id ? ' active' : ''}`}
        style={{ left: `${(x0 / size[0]) * 100}%`, top: `${(y0 / size[1]) * 100}%`, width: `${((x1 - x0) / size[0]) * 100}%`, height: `${((y1 - y0) / size[1]) * 100}%` }}
        title={`Область ${region.id}`}
        aria-label={`Область различий ${region.id}`}
        onClick={() => onPick(region.id)}
      />
    );
  });
}

function SheetPane({ title, sheet, image, imageError, zoom, regions, side, size, activeId, onPick, showBoxes, innerRef, onScroll }) {
  return (
    <section className="sheet-pane" aria-label={title}>
      <header className="sheet-pane-head">
        <span className={`stage-chip stage-${side === 'left' ? 'PD' : 'RD'}`}>{title}</span>
        <span className="small mono" title={sheet.filename}>{sheet.filename}</span>
        <span className="muted small">стр. {sheet.page}{sheet.sheet_no ? ` • лист ${sheet.sheet_no}` : ''}</span>
      </header>
      <div className="sheet-pane-body" ref={innerRef} onScroll={onScroll}>
        {imageError && <div className="error-banner"><span>{imageError}</span></div>}
        {!image && !imageError && <div className="empty-state"><span className="spinner" /></div>}
        {image && (
          <div className="sheet-canvas" style={{ width: `${zoom * 100}%` }}>
            <img src={image} alt={`${title}: ${sheet.title || 'лист'}`} draggable={false} />
            {showBoxes && <RegionBoxes regions={regions} side={side} size={size} activeId={activeId} onPick={onPick} />}
          </div>
        )}
      </div>
    </section>
  );
}

/**
 * "Сравнение листов": a viewing mode of the inspector's workplace. Two panels of the paired sheets, the difference overlay,
 * the picture similarity and the pairing confidence (two separate figures); no violation is claimed anywhere here.
 */
export default function SheetCompare({ processId }) {
  const [status, setStatus] = useState(null);
  const [kind, setKind] = useState('all');
  const [listing, setListing] = useState(null);
  const [listError, setListError] = useState('');
  const [loadingList, setLoadingList] = useState(false);
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState(null);
  const [result, setResult] = useState(null);
  const [resultError, setResultError] = useState('');
  const [loadingPair, setLoadingPair] = useState(false);
  const [images, setImages] = useState({ left: null, right: null, overlay: null });
  const [imageErrors, setImageErrors] = useState({});
  const [view, setView] = useState('panels');
  const [showBoxes, setShowBoxes] = useState(true);
  const [sync, setSync] = useState(true);
  const [zoom, setZoom] = useState(1);
  const [activeRegion, setActiveRegion] = useState(null);
  const leftRef = useRef(null);
  const rightRef = useRef(null);
  const syncing = useRef(false);
  const token = useRef(0);

  useEffect(() => {
    api.sheetCompareStatus().then(setStatus).catch(() => setStatus({ enabled: false, available: false }));
  }, []);

  const loadPairs = useCallback(async () => {
    if (!processId) return;
    setLoadingList(true);
    setListError('');
    try {
      setListing(await api.sheetPairs(processId, { kind, limit: 300 }));
    } catch (err) {
      setListing(null);
      setListError(err.message || 'Не удалось сопоставить листы');
    } finally {
      setLoadingList(false);
    }
  }, [processId, kind]);

  useEffect(() => {
    if (status?.enabled && status?.available) loadPairs();
  }, [status, loadPairs]);

  const openPair = useCallback(async (pair) => {
    const mine = ++token.current;
    setSelected(pair);
    setResult(null);
    setResultError('');
    setImages({ left: null, right: null, overlay: null });
    setImageErrors({});
    setActiveRegion(null);
    setLoadingPair(true);
    fetchPageImage(pair.left.document_id, pair.left.page).then((url) => mine === token.current && setImages((i) => ({ ...i, left: url })))
      .catch((err) => mine === token.current && setImageErrors((e) => ({ ...e, left: err.message })));
    fetchPageImage(pair.right.document_id, pair.right.page).then((url) => mine === token.current && setImages((i) => ({ ...i, right: url })))
      .catch((err) => mine === token.current && setImageErrors((e) => ({ ...e, right: err.message })));
    try {
      const data = await api.sheetCompare(pair);
      if (mine !== token.current) return;
      setResult(data);
      if (data.picture?.comparable) {
        fetchSheetOverlay(pair).then((url) => mine === token.current && setImages((i) => ({ ...i, overlay: url })))
          .catch((err) => mine === token.current && setImageErrors((e) => ({ ...e, overlay: err.message })));
      }
    } catch (err) {
      if (mine === token.current) setResultError(err.message || 'Не удалось сравнить листы');
    } finally {
      if (mine === token.current) setLoadingPair(false);
    }
  }, []);

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (listing?.pairs || []).filter((p) => !needle
      || [p.left.title, p.right.title, p.left.filename, p.right.filename, p.discipline].some((v) => String(v || '').toLowerCase().includes(needle)));
  }, [listing, search]);

  function mirror(from, to) {
    if (!sync || syncing.current || !from.current || !to.current) return;
    syncing.current = true;
    to.current.scrollLeft = from.current.scrollLeft;
    to.current.scrollTop = from.current.scrollTop;
    requestAnimationFrame(() => { syncing.current = false; });
  }

  if (!status) return <div className="empty-state"><span className="spinner" /> Проверяю режим…</div>;
  if (!status.enabled) {
    return <div className="card empty-state">Режим «Сравнение листов» выключен (флаг CASE10_SHEET_COMPARE_ENABLED). Это вспомогательный просмотр, он не влияет на находки.</div>;
  }
  if (!status.available) {
    return <div className="card empty-state">Для сравнения листов нужна библиотека OpenCV, в этой сборке её нет: режим отключён без влияния на остальную проверку.</div>;
  }

  const picture = result?.picture;
  const size = picture?.left_size_pt ? picture.left_size_pt : null;
  const sizeRight = picture?.right_size_pt ? picture.right_size_pt : null;
  const regions = picture?.regions || [];

  return (
    <div className="sheet-compare">
      <aside className="sheet-list card">
        <h3>Сравнение листов</h3>
        <p className="muted small">Только просмотр: различие рисунка не означает нарушение. Решение принимает инспектор.</p>
        <div className="seg" role="group" aria-label="Тип пар">
          {KIND_OPTIONS.map((o) => (
            <button key={o.key} type="button" className={`seg-btn${kind === o.key ? ' active' : ''}`} onClick={() => setKind(o.key)}>{o.label}</button>
          ))}
        </div>
        <input className="input" type="search" placeholder="Поиск по названию, файлу, разделу" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Поиск пар листов" />
        {loadingList && <div className="muted small"><span className="spinner" /> Сопоставляю листы (первый раз — до минуты, дальше из кэша)…</div>}
        {listError && <div className="error-banner"><span>{listError}</span></div>}
        {listing && (
          <div className="muted small">
            Пар: {listing.total}{rows.length !== listing.pairs.length ? `, показано ${rows.length}` : ''} • пар документов: {listing.document_pairs}
            {listing.truncated_document_pairs > 0 ? ` (ещё ${listing.truncated_document_pairs} пар документов не просмотрено)` : ''}
            {listing.truncated_documents?.length ? ' • у части документов просмотрены не все страницы' : ''}
          </div>
        )}
        {listing && !listing.pairs.length && !loadingList && (
          <div className="muted small">Пар листов нет: нужны документы ПД и РД одного раздела, листы с названием чертежа и общими признаками.</div>
        )}
        <ul className="sheet-pairs" role="listbox" aria-label="Пары листов">
          {rows.map((p) => (
            <li key={pairKey(p)}>
              <button type="button" className={`sheet-pair${selected && pairKey(selected) === pairKey(p) ? ' active' : ''}`} onClick={() => openPair(p)} role="option" aria-selected={Boolean(selected && pairKey(selected) === pairKey(p))}>
                <div className="sheet-pair-title">{p.right.title || p.left.title || 'Лист без названия'}</div>
                <div className="muted small">{KIND_LABEL[p.kind] || p.kind} • {p.discipline} • стр. {p.left.page} ↔ {p.right.page}</div>
                <div className="small">уверенность сопоставления <b>{conf(p.pair_confidence)}</b>{p.ambiguous ? ' • неоднозначно' : ''}</div>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <main className="sheet-view">
        {!selected && <div className="card empty-state">Выберите пару листов слева.</div>}
        {selected && (
          <>
            <div className="sheet-figures card" aria-label="Показатели пары">
              <div className="sheet-figure">
                <span className="muted small">Уверенность сопоставления</span>
                <b className="figure-value">{conf(result?.pairing?.confidence ?? selected.pair_confidence)}</b>
                <span className="muted small">{(result?.pairing?.basis || selected.basis || []).join('; ') || 'признаки сопоставления не вычислены'}</span>
                <span className="muted small">{Object.entries(result?.pairing?.components || selected.components || {}).map(([k, v]) => `${COMPONENT_LABELS[k] || k} ${v.toFixed(2)}`).join(' • ')}</span>
              </div>
              <div className="sheet-figure">
                <span className="muted small">Процент сходства листов</span>
                <b className="figure-value">{loadingPair ? '…' : pct(picture?.similarity)}</b>
                <span className="muted small">{picture?.alignment ? `выравнивание: ${picture.alignment.method}` : 'доля совпавших штрихов после выравнивания'}</span>
              </div>
              <div className="sheet-figure">
                <span className="muted small">Величина отклонения</span>
                <b className="figure-value">—</b>
                <span className="muted small">не вычисляется: по рисунку числа не считываются</span>
              </div>
            </div>

            {resultError && <div className="error-banner"><span>{resultError}</span></div>}
            {picture && !picture.comparable && <div className="warn-banner" role="status">{REASON_TEXT[picture.reason] || 'Листы нельзя сравнить по рисунку.'}</div>}
            {picture?.comparable && picture.layout_match && <div className={`layout-note layout-${picture.layout_match}`} role="status">{LAYOUT_TEXT[picture.layout_match]}</div>}
            {result?.pairing?.rejected && <div className="warn-banner" role="alert">Пара не подтверждается: {result.pairing.rejected}.</div>}
            {selected.ambiguous && <div className="warn-banner" role="status">Сопоставление неоднозначно: у листа есть близкий конкурент, проверьте пару глазами.</div>}

            <div className="sheet-toolbar" role="toolbar" aria-label="Просмотр пары листов">
              <div className="seg" role="group" aria-label="Вид">
                {VIEW_OPTIONS.map((o) => (
                  <button key={o.key} type="button" className={`seg-btn${view === o.key ? ' active' : ''}`} onClick={() => setView(o.key)}>{o.label}</button>
                ))}
              </div>
              <button type="button" className={`btn tiny${showBoxes ? '' : ' toggled'}`} onClick={() => setShowBoxes((v) => !v)} disabled={!regions.length}>
                {showBoxes ? 'Скрыть рамки' : 'Показать рамки'}
              </button>
              <button type="button" className={`btn tiny${sync ? ' toggled' : ''}`} onClick={() => setSync((v) => !v)} title="Общая прокрутка панелей">
                {sync ? 'Синхронно' : 'Независимо'}
              </button>
              <button type="button" className="btn tiny" onClick={() => setZoom((z) => Math.max(1, +(z / 1.5).toFixed(2)))} aria-label="Уменьшить">−</button>
              <span className="zoom-value">×{zoom.toFixed(1)}</span>
              <button type="button" className="btn tiny" onClick={() => setZoom((z) => Math.min(6, +(z * 1.5).toFixed(2)))} aria-label="Увеличить">+</button>
              <span className="muted small">Области различий: {regions.length}{picture?.diffuse_share > 0 ? ` • вне областей ${pct(picture.diffuse_share)} различий` : ''}</span>
            </div>

            {view === 'panels' && (
              <div className="sheet-panes">
                <SheetPane title="Лист слева" side="left" sheet={selected.left} image={images.left} imageError={imageErrors.left} zoom={zoom} regions={regions} size={size}
                  activeId={activeRegion} onPick={setActiveRegion} showBoxes={showBoxes} innerRef={leftRef} onScroll={() => mirror(leftRef, rightRef)} />
                <SheetPane title="Лист справа" side="right" sheet={selected.right} image={images.right} imageError={imageErrors.right} zoom={zoom} regions={regions} size={sizeRight}
                  activeId={activeRegion} onPick={setActiveRegion} showBoxes={showBoxes} innerRef={rightRef} onScroll={() => mirror(rightRef, leftRef)} />
              </div>
            )}
            {view === 'overlay' && (
              <section className="sheet-pane sheet-overlay" aria-label="Наложение различий">
                <header className="sheet-pane-head">
                  <span className="pill">наложение</span>
                  <span className="small"><span className="swatch swatch-left" /> только на левом листе <span className="swatch swatch-right" /> только на правом <span className="swatch swatch-both" /> совпадает</span>
                </header>
                <div className="sheet-pane-body">
                  {imageErrors.overlay && <div className="error-banner"><span>{imageErrors.overlay}</span></div>}
                  {!images.overlay && !imageErrors.overlay && picture?.comparable !== false && <div className="empty-state"><span className="spinner" /></div>}
                  {images.overlay && (
                    <div className="sheet-canvas" style={{ width: `${zoom * 100}%` }}>
                      <img src={images.overlay} alt="Наложение различий сопоставленных листов" draggable={false} />
                      {showBoxes && <RegionBoxes regions={regions} side="left" size={size} activeId={activeRegion} onPick={setActiveRegion} />}
                    </div>
                  )}
                </div>
              </section>
            )}

            {regions.length > 0 && (
              <section className="sheet-regions card" aria-label="Области различий">
                <h4>Области различий (по убыванию заметности)</h4>
                <ul>
                  {regions.slice(0, 12).map((r) => (
                    <li key={r.id}>
                      <button type="button" className={`link-btn${activeRegion === r.id ? ' active' : ''}`} onClick={() => setActiveRegion(r.id)}>
                        № {r.id}: {r.kind === 'only_left' ? 'штрихи только слева' : r.kind === 'only_right' ? 'штрихи только справа' : 'штрихи различаются'} • ≈ {(r.area_mm2 / 100).toFixed(0)} см² • доля {pct(r.share)}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </main>
    </div>
  );
}
