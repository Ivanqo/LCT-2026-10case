#!/usr/bin/env node
// Final / step C1: scripted walkthrough of the inspector card against the experts' checklist, a screenshot per step.
// Same plumbing as s5_walkthrough.mjs (local Chromium over the DevTools protocol, Node's built-in WebSocket, no npm deps).
//
//   node frontend_react/scripts/c1_walkthrough.mjs --out evaluation/phase12/card_screens \
//        [--app http://localhost:5173] [--api http://127.0.0.1:8080] [--chrome <path to chrome.exe>]
//
// Expects a FRESH demo stack (s5_demo_serve.py --reset) and the Vite dev server. Demo credentials come from
// S5_DEMO_LOGIN / S5_DEMO_PASSWORD (local demo database only). It decides candidates and finalizes the demo protocol.
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir, homedir } from 'node:os';
import { join, resolve } from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, cur, i, all) => (cur.startsWith('--') ? [...acc, [cur.slice(2), all[i + 1]]] : acc), []));
const OUT = resolve(args.out || 'evaluation/phase12/card_screens');
const APP = (args.app || 'http://localhost:5173').replace(/\/$/, '');
const API = (args.api || 'http://127.0.0.1:8080').replace(/\/$/, '');
const CHROME = args.chrome || join(homedir(), 'AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe');
const PORT = 9333;
const W = 1600;
const H = 1000;
mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// ---------------------------------------------------------------- CDP plumbing
if (!existsSync(CHROME)) throw new Error(`Chromium not found: ${CHROME} (pass --chrome)`);
const profile = mkdtempSync(join(tmpdir(), 's5-cdp-'));
const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`, '--hide-scrollbars', '--no-first-run', '--lang=ru-RU', 'about:blank'], { stdio: 'ignore' });
process.on('exit', () => chrome.kill());

let targets;
for (let i = 0; i < 50; i += 1) {
  try {
    targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    if (targets.some((t) => t.type === 'page')) break;
  } catch { /* not up yet */ }
  await sleep(200);
}
const page = targets.find((t) => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let seq = 0;
const pending = new Map();
const listeners = [];
ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve: ok, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(`${msg.error.message} ${msg.error.data || ''}`));
    else ok(msg.result);
  } else if (msg.method) listeners.forEach((fn) => fn(msg));
});
const send = (method, params = {}) => new Promise((ok, reject) => {
  seq += 1;
  pending.set(seq, { resolve: ok, reject });
  ws.send(JSON.stringify({ id: seq, method, params }));
});
const once = (method) => new Promise((ok) => {
  const fn = (msg) => {
    if (msg.method === method) {
      listeners.splice(listeners.indexOf(fn), 1);
      ok(msg.params);
    }
  };
  listeners.push(fn);
});

async function evaluate(expression) {
  const res = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (res.exceptionDetails) throw new Error(`eval failed: ${res.exceptionDetails.text} :: ${expression.slice(0, 120)}`);
  return res.result.value;
}
async function waitFor(expression, timeout = 20000, what = expression) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      if (await evaluate(expression)) return;
    } catch { /* page reloading */ }
    await sleep(150);
  }
  let seen = ""; try { seen = await evaluate("document.body.innerText.slice(0,300)"); } catch { /* ignore */ }
  throw new Error(`timeout waiting for ${what}; page shows: ${seen}`);
}
async function navigate(url) {
  const loaded = once('Page.loadEventFired');
  await send('Page.navigate', { url });
  await loaded;
}
let shotNo = 0;
const shots = [];
async function shot(name, caption) {
  await sleep(400);
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  shotNo += 1;
  const file = `${String(shotNo).padStart(2, '0')}_${name}.png`;
  writeFileSync(join(OUT, file), Buffer.from(data, 'base64'));
  shots.push({ file, caption });
  log('shot', file);
}
async function mouse(type, x, y, extra = {}) {
  await send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1, ...extra });
}
async function click(x, y) {
  await mouse('mouseMoved', x, y, { button: 'none' });
  await mouse('mousePressed', x, y);
  await mouse('mouseReleased', x, y);
}
/** Real mouse click on the first element matching `selector` whose text contains `text` (scrolled into view). */
async function clickText(selector, text, { exact = false } = {}) {
  const pos = await evaluate(`(() => {
    const els = [...document.querySelectorAll(${JSON.stringify(selector)})];
    const el = els.find((e) => ${exact ? `e.textContent.trim() === ${JSON.stringify(text)}` : `e.textContent.includes(${JSON.stringify(text)})`});
    if (!el) return null;
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  })()`);
  if (!pos) throw new Error(`no ${selector} with text ${text}`);
  await sleep(150);
  await click(pos[0], pos[1]);
  await sleep(300);
}
async function key(k, { code, vk, ctrl = false, text } = {}) {
  const base = { key: k, code: code || (k.length === 1 ? `Key${k.toUpperCase()}` : k), windowsVirtualKeyCode: vk || (k.length === 1 ? k.toUpperCase().charCodeAt(0) : 0), modifiers: ctrl ? 2 : 0 };
  await send('Input.dispatchKeyEvent', { type: text ? 'keyDown' : 'rawKeyDown', ...base, text });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
  await sleep(250);
}
async function typeText(text) {
  await send('Input.insertText', { text });
  await sleep(200);
}
async function focusAndType(selector, text) {
  await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); el.scrollIntoView({block:'center'}); el.focus(); return true; })()`);
  await typeText(text);
}
async function scrollTo(selector, block = 'start') {
  await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (el) el.scrollIntoView({ block: ${JSON.stringify(block)} }); return !!el; })()`);
  await sleep(500);
}
async function drag(from, to) {
  await mouse('mouseMoved', from[0], from[1], { button: 'none' });
  await mouse('mousePressed', from[0], from[1]);
  for (let i = 1; i <= 6; i += 1) {
    await mouse('mouseMoved', from[0] + ((to[0] - from[0]) * i) / 6, from[1] + ((to[1] - from[1]) * i) / 6);
  }
  await mouse('mouseReleased', to[0], to[1]);
  await sleep(300);
}
const heading = () => evaluate("document.querySelector('.candidate-title')?.textContent || ''");
/** Tick / untick the queue checkbox of the row whose text contains `text` (or the selected row) and wait for it. */
async function setChecked(text, wanted) {
  const find = text === null
    ? "document.querySelector('.queue-row.selected input')"
    : `[...document.querySelectorAll('.queue-row')].find((r) => r.textContent.includes(${JSON.stringify(text)}))?.querySelector('input')`;
  const state = await evaluate(`(() => { const b = ${find}; return b ? b.checked : null; })()`);
  if (state === null) return false;
  if (state !== wanted) {
    const pos = await evaluate(`(() => { const b = ${find}; b.scrollIntoView({ block: 'center' }); const r = b.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`);
    await click(pos[0], pos[1]);
    await waitFor(`(${find})?.checked === ${wanted}`, 5000, `checkbox ${text} -> ${wanted}`);
  }
  return true;
}


// ---------------------------------------------------------------- scenario
await send('Page.enable');
await send('Runtime.enable');
listeners.push((msg) => {
  if (msg.method === 'Runtime.exceptionThrown') log('PAGE EXCEPTION', JSON.stringify(msg.params.exceptionDetails).slice(0, 600));
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') log('console.error', JSON.stringify(msg.params.args.map((a) => a.value || a.description)).slice(0, 400));
});
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });

for (let i = 0; ; i += 1) { // the gateway answers before the Python API has booted
  try {
    if ((await (await fetch(`${API}/health`)).json()).status === 'ok') break;
  } catch { /* not up yet */ }
  if (i > 160) throw new Error(`API behind ${API} is not healthy`);
  await sleep(500);
}
const login = await (await fetch(`${API}/api/auth/login`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ login: process.env.S5_DEMO_LOGIN || 'admin', password: process.env.S5_DEMO_PASSWORD || 'S5demo!2026' }),
})).json();
const token = login.token || login.access_token;
const auth = { Authorization: `Bearer ${token}` };
const api = async (path, opts = {}) => {
  const res = await fetch(`${API}${path}`, { ...opts, headers: { ...auth, 'Content-Type': 'application/json', ...(opts.headers || {}) } });
  const text = await res.text();
  let body = null;
  try { body = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status, body, text, type: res.headers.get('content-type') };
};
const projects = (await api('/api/projects')).body;
const tyu = projects.find((p) => p.name.includes('Тюменская'));
const alt = projects.find((p) => p.name.includes('Алтуфьевское'));
const processOf = async (projectId) => (await api(`/api/case10/processes?project_id=${projectId}`)).body[0].process_id;
const tyuProcess = await processOf(tyu.id);
const altProcess = await processOf(alt.id);
const queue = (await api(`/api/case10/processes/${tyuProcess}/workbench`)).body;
if (queue.items.filter((i) => i.finding_status === 'CANDIDATE').length !== 6) throw new Error('demo DB is not fresh: restart s5_demo_serve.py with --reset');
const checks = {};
const note = (key, value) => { checks[key] = value; log('check', key, JSON.stringify(value)); };

let signedIn = false;
const boot = async (projectId) => {
  if (!signedIn) { // sign in through the form like a user would (demo credentials, local demo database)
    await navigate(`${APP}/`);
    await waitFor("!!document.querySelector('.auth-card')", 20000, 'login form');
    await focusAndType('.auth-card input[type="text"], .auth-card input:not([type])', process.env.S5_DEMO_LOGIN || 'admin');
    await focusAndType('.auth-card input[type="password"]', process.env.S5_DEMO_PASSWORD || 'S5demo!2026');
    await clickText('.auth-card button', 'Войти');
    await waitFor("!!document.querySelector('.topbar')", 20000, 'signed in');
    signedIn = true;
  }
  await evaluate(`localStorage.setItem('case10_react_project_id', '${projectId}'); true`);
  await navigate(`${APP}/`);
  await waitFor("!!document.querySelector('.topbar')", 20000, 'app shell');
  await clickText('.tab', 'Проверка');
  await waitFor("!!document.querySelector('.queue') && !!document.querySelector('.progress-panel')", 40000, 'workbench + progress');
};
const imagesReady = () => waitFor("[...document.querySelectorAll('.stage-panel img')].every((i) => i.complete)", 150000, 'panel images');
const rowText = () => evaluate("document.querySelector('.queue-row.selected')?.textContent || ''");

// ---- Tyumen: candidates with ПД+РД, no ИД
await boot(tyu.id);
await waitFor("!!document.querySelector('.stage-panel img')", 150000, 'first panel image');
await imagesReady();
await shot('workbench_overview', 'Окно проверки: прогресс (документы/листы, неполный охват), очередь с поиском и фильтрами, карточка: основание, раздельные уверенности, запись протокола');
await scrollTo('.progress-panel .btn', 'center');
await clickText('.progress-panel button', 'Подробнее');
await shot('progress_incomplete', 'Прогресс проверки: документы и листы по стадиям, пропуски с причинами, пометка «Охват неполный»');
await evaluate("document.querySelector('.content').scrollTo({top:0}); true");

// explanation block + identity + protocol entry
await scrollTo('.explain-block', 'start');
await clickText('.protocol-entry button', 'Перейти к записи протокола');
await waitFor("!!document.querySelector('.protocol-entry dl')", 20000, 'protocol entry');
await shot('explanation_protocol_entry', 'Основание расхождения: правило, порог, отклонение («не вычислялось», если не считалось), уверенности раздельно, вероятность не заявляется; переход к записи протокола и экспорт');
note('explanation_text', await evaluate("document.querySelector('.explain-block')?.innerText.slice(0, 900) || ''"));

// panels: pair selector, missing ИД placeholder with «Дозагрузить», sheet designation, stage provenance
await scrollTo('.panels-toolbar', 'start');
await imagesReady();
await shot('panels_pair_missing_id', 'Пара «ПД / РД и ИД»: у отсутствующей ИД — плейсхолдер с рекомендацией и «Дозагрузить»; в шапке панелей — лист, источник стадии');
note('panel_meta_pd', await evaluate("document.querySelector('.stage-panel.stage-PD .meta-grid')?.innerText || ''"));
note('panel_meta_rd', await evaluate("document.querySelector('.stage-panel.stage-RD .meta-grid')?.innerText || ''"));
await clickText('.stage-panel.missing button', 'Дозагрузить');
await waitFor("!!document.querySelector('.modal-box[aria-label=\"Дозагрузить документы\"]')", 5000, 'supplement dialog');
await shot('supplement_dialog', '«Дозагрузить»: загрузка недостающих документов из карточки; решения сохраняются, пересчитываются только затронутые параметры');
await clickText('.modal-box button', 'Закрыть', { exact: true });
await clickText('.seg-btn', 'ПД / РД', { exact: true });
await imagesReady();
await shot('pair_pd_rd', 'Явный выбор пары «ПД / РД»: ИД не показывается');
await clickText('.seg-btn', 'ПД / РД и ИД', { exact: true });

// independent panels + hidden frames + labels
await clickText('.panels-toolbar button', 'Панели синхронны');
await clickText('.stage-panel.stage-PD .stage-toolbar button', '+');
await imagesReady();
note('independent_zoom', await evaluate("[...document.querySelectorAll('.stage-panel .zoom-value')].map((e) => e.textContent)"));
await shot('independent_panels', 'Панели независимы: у ПД свой масштаб, у РД свой; метки «значение · стадия» на рамках');
note('bbox_labels', await evaluate("[...document.querySelectorAll('.bbox-label')].map((e) => e.textContent)"));
await clickText('.panels-toolbar button', 'Скрыть рамки');
note('frames_hidden_count', await evaluate("document.querySelectorAll('.bbox-overlay').length"));
await shot('frames_hidden', 'Рамки скрыты кнопкой; повторное нажатие возвращает их на то же место (координаты в долях листа)');
await clickText('.panels-toolbar button', 'Показать рамки');
note('frames_shown_count', await evaluate("document.querySelectorAll('.bbox-overlay').length"));
await clickText('.panels-toolbar button', 'Панели независимы');

// page switch
await clickText('.stage-panel.stage-PD .page-switch button', '›');
await imagesReady();
await shot('page_switch', 'Переключение страниц внутри документа (стр. ПД +1): «к фрагменту» возвращает к исходной области');
note('pd_page_after_switch', await evaluate("document.querySelector('.stage-panel.stage-PD .page-input')?.value"));
await clickText('.stage-panel.stage-PD .page-switch button', 'к фрагменту');

// full screen
await clickText('.panels-toolbar button', 'Во весь экран');
await sleep(800);
note('fullscreen_state', await evaluate("({native: !!document.fullscreenElement, pseudo: !!document.querySelector('.panels-wrap.is-full')})"));
await shot('fullscreen', 'Панели во весь экран (рамки, переключатели и пара остаются доступны)');
await clickText('.panels-toolbar button', 'Выйти из полного экрана');
await sleep(500);

// snapshot of fragment (download triggered; assert the button works without error)
note('snapshot_button_enabled', await evaluate("!![...document.querySelectorAll('.stage-toolbar button')].find((b) => b.textContent.includes('Снимок') && !b.disabled)"));

// decision bar: clarification names what to clarify; rejection without a reason is not saved
await evaluate("document.querySelector('.content').scrollTo({top:0}); true");
await scrollTo('.decision-bar', 'center');
await key('r');
note('reject_save_disabled_without_comment', await evaluate("[...document.querySelectorAll('.decision-form button')].find((b) => b.textContent.includes('Сохранить'))?.disabled"));
await key('Escape', { code: 'Escape', vk: 27 });
await key('u');
await shot('clarify_kind', 'Уточнить: выбор «что именно» (не хватает документа / неверный источник / другое) и обязательное пояснение');
await key('Escape', { code: 'Escape', vk: 27 });
const noReason = await api(`/api/case10/evidence-groups/${queue.items.find((i) => i.finding_status === 'CANDIDATE').id}/decisions`, { method: 'POST', body: JSON.stringify({ decision: 'Reject', reason_code: 'NOT_A_VIOLATION', comment: '' }) });
note('api_reject_without_comment', { status: noReason.status, detail: noReason.body?.detail });

// split into parts (compound M-078, 3 systems)
await scrollTo('.decision-bar', 'center');
await key('s');
await waitFor("!!document.querySelector('.parts-panel table')", 10000, 'parts panel');
const setPart = async (label, decision, comment) => {
  await evaluate(`(() => { const sel = document.querySelector('select[aria-label="Решение по части ${label}"]'); const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set; setter.call(sel, ${JSON.stringify(decision)}); sel.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
  if (comment) await focusAndType(`input[aria-label="Причина по части ${label}"]`, comment);
};
await setPart('V2.7', 'Confirm');
await setPart('V2.8', 'Reject', 'В РД система V2.8 перенесена на соседний лист');
await shot('parts_selected', '«Разделить на части»: решение по каждой части; для отклонения причина обязательна');
await clickText('.parts-panel button', 'Сохранить решения по частям');
await waitFor("document.querySelector('.parts-panel h3')?.textContent.includes('решено 2 из 3')", 15000, 'two of three');
await scrollTo('.parts-panel', 'start');
await shot('parts_partial', 'Частичное решение сохранено (2 из 3): замечание остаётся без итогового решения, в очереди — «по частям 2/3»');
note('queue_partial_flag', await evaluate("[...document.querySelectorAll('.queue-row .flag')].map((e) => e.textContent).filter((t) => t.includes('по частям'))"));
await setPart('V2.9', 'Confirm');
await clickText('.parts-panel button', 'Сохранить решения по частям');
await waitFor("!document.querySelector('.parts-panel')", 20000, 'parts closed after overall decision');
await evaluate("document.querySelector('.content').scrollTo({top:0}); true");
const afterParts = await api(`/api/case10/processes/${tyuProcess}/workbench`);
const decidedGroup = afterParts.body.items.find((i) => i.compound_parts > 0 && i.inspector_status === 'CONFIRMED');
note('parts_overall_decision', decidedGroup ? { id: decidedGroup.id, finding_status: decidedGroup.finding_status, inspector_status: decidedGroup.inspector_status, parts: `${decidedGroup.parts_decided}/${decidedGroup.compound_parts}` } : null);
await shot('parts_complete', 'Все части решены: итоговое решение записано обычным путём (список, карточка и протокол согласованы)');

// page reload keeps the open record and filters
await evaluate("(() => { const input = document.querySelector('.queue-filters input'); input.focus(); return true; })()");
const beforeReload = await heading();
await navigate(`${APP}/`);
await waitFor("!!document.querySelector('.topbar')", 20000);
await clickText('.tab', 'Проверка');
await waitFor("!!document.querySelector('.candidate-title')", 40000, 'card after reload');
const afterReload = await heading();
note('reload_keeps_selection', { before: beforeReload, after: afterReload, same: beforeReload === afterReload });
await shot('after_reload', 'После обновления страницы: открыта та же запись, решения на месте (хранятся в базе), режимы просмотра восстановлены');

// ---- Altuf: states without a result
await boot(alt.id);
await clickText('.queue-mode', 'Все');
const altQueue = (await api(`/api/case10/processes/${altProcess}/workbench`)).body;
const byState = {};
for (const item of altQueue.items) if (item.no_result && !byState[item.no_result]) byState[item.no_result] = item;
note('no_result_states_in_demo', Object.fromEntries(Object.entries(byState).map(([k, v]) => [k, `${v.matrix_code} ${v.finding_status}`])));
let n = 0;
for (const [state, item] of Object.entries(byState)) {
  await focusAndType('.queue-filters input', item.matrix_code);
  await sleep(300);
  await waitFor("!!document.querySelector('.queue-row')", 5000);
  await clickText('.queue-row', item.matrix_code);
  await waitFor("!!document.querySelector('.no-result')", 20000, `no-result for ${state}`);
  n += 1;
  await scrollTo('.no-result', 'center');
  await shot(`no_result_${state.toLowerCase()}`, `Состояние «${state}»: причина и доступное действие (${item.matrix_code})`);
  note(`no_result_${state}`, await evaluate("document.querySelector('.no-result')?.innerText"));
  await evaluate(`(() => { const input = document.querySelector('.queue-filters input'); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; setter.call(input, ''); input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
}

// ---- Tyumen again: finalization warning (ИД отсутствует), then locked
await boot(tyu.id);
const toDecide = (await api(`/api/case10/processes/${tyuProcess}/workbench`)).body.items.filter((i) => i.finding_status === 'CANDIDATE' && i.inspector_status === 'PENDING');
for (const item of toDecide) {
  await api(`/api/case10/evidence-groups/${item.id}/decisions`, { method: 'POST', body: JSON.stringify({ decision: 'Confirm', comment: 'Подтверждено при проверке листов ПД/РД' }) });
}
await navigate(`${APP}/`);
await waitFor("!!document.querySelector('.topbar')", 20000);
await clickText('.tab', 'Проверка');
await waitFor("!!document.querySelector('.progress-panel')", 40000);
await evaluate("document.querySelector('.content').scrollTo({top:0}); true");
await clickText('button', 'Финализировать протокол');
await waitFor("!!document.querySelector('.finalize-warning')", 10000, 'ИД warning');
note('finalize_disabled_before_ack', await evaluate("[...document.querySelectorAll('.modal-box button')].find((b) => b.textContent.trim() === 'Финализировать')?.disabled"));
await shot('finalize_warning_no_id', 'Финализация: предупреждение «ИД не загружена» — завершить можно только осознанно (отметка)');
await clickText('.finalize-warning label', 'Я понимаю');
note('finalize_disabled_after_ack', await evaluate("[...document.querySelectorAll('.modal-box button')].find((b) => b.textContent.trim() === 'Финализировать')?.disabled"));
await clickText('.modal-box button', 'Финализировать', { exact: true });
await waitFor("!!document.querySelector('.finalize-banner')", 30000, 'finalized');
await sleep(800);
await shot('finalized_view_mode', 'После финализации: режим просмотра, решения и дозагрузка заблокированы');
const protocol = (await api(`/api/case10/protocols/current?project_id=${tyu.id}&process_id=${tyuProcess}`)).body;
const someGroup = queue.items[0].id;
const afterDecision = await api(`/api/case10/evidence-groups/${someGroup}/decisions`, { method: 'POST', body: JSON.stringify({ decision: 'Reject', reason_code: 'OTHER', comment: 'попытка после финализации' }) });
const afterParts409 = await api(`/api/case10/evidence-groups/${decidedGroup.id}/partial-decision`, { method: 'POST', body: JSON.stringify({ parts: [{ key: 's:V2.7', decision: 'Reject', comment: 'после финализации' }] }) });
note('after_finalize_decision_status', afterDecision.status);
note('after_finalize_partial_status', afterParts409.status);
const pdf = await fetch(`${API}/api/case10/protocols/${protocol.id}/pdf`, { headers: auth });
const submission = await api(`/api/case10/protocols/${protocol.id}/submission`);
note('protocol_pdf', { status: pdf.status, type: pdf.headers.get('content-type'), bytes: (await pdf.arrayBuffer()).byteLength });
note('protocol_submission', { status: submission.status, keys: Object.keys(submission.body || {}).slice(0, 8) });
const entry = await api(`/api/case10/evidence-groups/${decidedGroup.id}/protocol-entry`);
note('protocol_entry_of_split_finding', { status: entry.status, section: entry.body?.section, decision: entry.body?.decision?.decision, rationale: entry.body?.decision_rationale, parts: (entry.body?.parts?.parts || []).map((p) => `${p.label}:${p.decision}`) });
const unfinalize = await api(`/api/case10/protocols/${protocol.id}/unfinalize`, { method: 'POST', body: JSON.stringify({ reason: '' }) });
note('unfinalize_without_reason_status', unfinalize.status);

writeFileSync(join(OUT, 'checks.json'), JSON.stringify(checks, null, 1));
writeFileSync(join(OUT, 'shots.json'), JSON.stringify(shots, null, 1));
log('done', shots.length, 'screenshots ->', OUT);
ws.close();
chrome.kill();
process.exit(0);
