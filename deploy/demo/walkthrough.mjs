#!/usr/bin/env node
// C2a: scripted walkthrough of the DEMO stand against the six "checks on your own package" of the experts' written
// feedback (p. 7) plus the two functions never run in a browser before: «Преобразовать в кандидата» and «Дозагрузить»
// through the real file picker (DOM.setFileInputFiles). Screenshots + checks.json land in --out.
//
//   python deploy/demo/demo.py prepare ... && python deploy/demo/demo.py start      # the stand, backup projects
//   node deploy/demo/walkthrough.mjs --out evaluation/phase12/demo_screens [--app http://127.0.0.1:3100] [--api http://127.0.0.1:8080]
//
// It MUTATES the stand (decisions, finalization, one supplement upload): run `demo.py reset` afterwards. Same plumbing
// as frontend_react/scripts/c1_walkthrough.mjs (local Chromium over DevTools, Node built-in WebSocket, no npm deps).
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir, homedir } from 'node:os';
import { join, resolve } from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, cur, i, all) => (cur.startsWith('--') ? [...acc, [cur.slice(2), all[i + 1]]] : acc), []));
const OUT = resolve(args.out || 'evaluation/phase12/demo_screens');
const APP = (args.app || 'http://127.0.0.1:3100').replace(/\/$/, '');
const API = (args.api || 'http://127.0.0.1:8080').replace(/\/$/, '');
const CHROME = args.chrome || join(homedir(), 'AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe');
const PORT = Number(args.cdp || 9334);
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

// ---------------------------------------------------------------- scenario
await send('Page.enable');
await send('Runtime.enable');
await send('DOM.enable');
listeners.push((msg) => {
  if (msg.method === 'Runtime.exceptionThrown') log('PAGE EXCEPTION', JSON.stringify(msg.params.exceptionDetails).slice(0, 600));
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') log('console.error', JSON.stringify(msg.params.args.map((a) => a.value || a.description)).slice(0, 300));
});
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });

const LOGIN = process.env.CASE10_DEMO_LOGIN || 'admin';
const PASSWORD = process.env.CASE10_DEMO_PASSWORD || 'Case10-Demo-2026';
for (let i = 0; ; i += 1) {
  try { if ((await (await fetch(`${API}/health`)).json()).status === 'ok') break; } catch { /* not up yet */ }
  if (i > 160) throw new Error(`API behind ${API} is not healthy`);
  await sleep(500);
}
const loginRes = await (await fetch(`${API}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ login: LOGIN, password: PASSWORD }) })).json();
const auth = { Authorization: `Bearer ${loginRes.token || loginRes.access_token}` };
const api = async (path, opts = {}) => {
  const res = await fetch(`${API}${path}`, { ...opts, headers: { ...auth, 'Content-Type': 'application/json', ...(opts.headers || {}) } });
  const text = await res.text();
  let body = null;
  try { body = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status, body, text, type: res.headers.get('content-type') };
};
const projects = (await api('/api/projects')).body;
const projectOf = (needle) => {
  const p = projects.find((x) => x.name.includes(needle));
  if (!p) throw new Error(`project with "${needle}" not found; run demo.py prepare`);
  return p;
};
const processOf = async (projectId) => (await api(`/api/case10/processes?project_id=${projectId}`)).body[0].process_id;
const queueOf = async (processId) => (await api(`/api/case10/processes/${processId}/workbench`)).body;
const tyu = projectOf('Тюмень');
const nov = projectOf('Новослободская');
const hyp = projectOf('свободный поиск');
const tyuProcess = await processOf(tyu.id);
const novProcess = await processOf(nov.id);
const hypProcess = await processOf(hyp.id);
const checks = {};
const note = (key, value) => { checks[key] = value; log('check', key, JSON.stringify(value).slice(0, 400)); };

let signedIn = false;
const boot = async (projectId) => {
  if (!signedIn) { // sign in through the form like a user would
    await navigate(`${APP}/`);
    await waitFor("!!document.querySelector('.auth-card')", 20000, 'login form');
    await focusAndType('.auth-card input[type="text"], .auth-card input:not([type])', LOGIN);
    await focusAndType('.auth-card input[type="password"]', PASSWORD);
    await clickText('.auth-card button', 'Войти');
    await waitFor("!!document.querySelector('.topbar')", 20000, 'signed in');
    signedIn = true;
  }
  await evaluate(`localStorage.setItem('case10_react_project_id', '${projectId}'); true`);
  await navigate(`${APP}/`);
  await waitFor("!!document.querySelector('.topbar')", 20000, 'app shell');
  await clickText('.tab', 'Проверка');
  await waitFor("!!document.querySelector('.queue') && !!document.querySelector('.progress-panel')", 60000, 'workbench + progress');
};
const imagesReady = () => waitFor("[...document.querySelectorAll('.stage-panel img')].every((i) => i.complete)", 150000, 'panel images');
const setFilter = async (value) => {
  await evaluate(`(() => { const input = document.querySelector('.queue-filters input'); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; setter.call(input, ${JSON.stringify(value)}); input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
  await sleep(400);
};
const top = () => evaluate("document.querySelector('.content')?.scrollTo({top:0}); true");
const snapshotOf = (items) => Object.fromEntries(items.map((i) => [`${i.matrix_code}|${i.location || ''}|${i.hypothesis ? 'H' : ''}`, `${i.finding_status}|${i.evidence_tier || ''}|${i.expected}|${i.actual}`]));

// ================= Tyumen: checks 1, 2, 3, 5
await boot(tyu.id);
const tyuQueue = await queueOf(tyuProcess);
const tiers = {};
for (const item of tyuQueue.items) if (item.evidence_tier) tiers[item.evidence_tier] = (tiers[item.evidence_tier] || 0) + 1;
note('tyu_tiers', tiers);
await clickText('.queue-mode', 'Кандидаты');
await setFilter('142'); // a room the organizer's examples mark as a real discrepancy (rehearsal choice, not code logic)
await waitFor("!!document.querySelector('.queue-row')", 10000, 'queue row 142');
await clickText('.queue-row', 'M-078');
await waitFor("!!document.querySelector('.stage-panel img')", 150000, 'first panel image');
await imagesReady();
await shot('01_overview', 'Окно проверки: список кандидатов с уровнем выдачи, карточка, панели ПД и РД с рамками; ИД не загружена');

// check 1: pages of every available stage open with frames
await waitFor("document.querySelectorAll('.stage-panel .bbox-overlay').length >= 2", 30000, 'frames on the panels');
note('check1_panels', await evaluate(`[...document.querySelectorAll('.stage-panel')].map((p) => ({cls: p.className, frames: p.querySelectorAll('.bbox-overlay').length, labels: [...p.querySelectorAll('.bbox-label')].map((e) => e.textContent)}))`));
// check 3: object / element / stage and its source are visible
note('check3_identity', await evaluate("document.querySelector('.identity-strip')?.innerText.replace(/\\s+/g, ' ') || ''"));
note('check3_stage_source', await evaluate("[...document.querySelectorAll('.stage-panel .meta-grid')].map((e) => e.innerText.replace(/\\s+/g, ' ').slice(0, 400))"));
await scrollTo('.panels-toolbar', 'start');
await imagesReady();
await shot('02_panels_stage_sources', 'Панели: стадия определена по реестру/файлу (источник показан), файл, редакция, страница, лист, область; у отсутствующей ИД — причина и «Дозагрузить»');

// check 2: small print readable at zoom, return to the source area, labels stay in place
await clickText('.panels-toolbar button', 'Панели синхронны'); // toggles to independent
for (let i = 0; i < 3; i += 1) await clickText('.stage-panel.stage-PD .stage-toolbar button', '+');
await imagesReady();
note('check2_zoom', await evaluate("[...document.querySelectorAll('.stage-panel .zoom-value')].map((e) => e.textContent)"));
note('check2_frames_after_zoom', await evaluate("document.querySelectorAll('.stage-panel.stage-PD .bbox-overlay').length"));
await shot('03_zoomed', 'Увеличение листа ПД (панели независимы): рамка и метка «значение · стадия» остаются на месте');
await clickText('.stage-panel.stage-PD .stage-toolbar button', 'Фрагмент');
await sleep(500);
await shot('04_back_to_fragment', 'Возврат к исходной области кнопкой «Фрагмент»');
await clickText('.panels-toolbar button', 'Скрыть рамки');
note('check2_frames_hidden', await evaluate("document.querySelectorAll('.bbox-overlay').length"));
await clickText('.panels-toolbar button', 'Показать рамки');
note('check2_frames_shown', await evaluate("document.querySelectorAll('.bbox-overlay').length"));
await clickText('.panels-toolbar button', 'Панели независимы');

// explanation: separate confidences, no probability
await scrollTo('.explain-block', 'start');
await shot('05_explanation', 'Основание расхождения: правило, величина, отдельные уверенности (текст / привязка / сходство); вероятность не заявляется');
note('explanation_text', await evaluate("document.querySelector('.explain-block')?.innerText.slice(0, 900) || ''"));

// check 5: rejection without a reason is not saved; statuses agree list / card / protocol / after reload
await top();
await scrollTo('.decision-bar', 'center');
await key('r');
note('check5_reject_save_disabled_without_comment', await evaluate("[...document.querySelectorAll('.decision-form button')].find((b) => b.textContent.includes('Сохранить'))?.disabled"));
await key('Escape', { code: 'Escape', vk: 27 });
const target = tyuQueue.items.find((i) => i.matrix_code === 'M-078' && i.location === '142');
const noReason = await api(`/api/case10/evidence-groups/${target.id}/decisions`, { method: 'POST', body: JSON.stringify({ decision: 'Reject', reason_code: 'NOT_A_VIOLATION', comment: '' }) });
note('check5_api_reject_without_comment', { status: noReason.status });
await key('c');
await sleep(500);
await evaluate("(() => { const ta = document.querySelector('.decision-form textarea'); if (ta) ta.focus(); return !!ta; })()");
await typeText('Расхождение подтверждено по листам ПД и РД: система V2.5 в РД не показана');
await shot('06_confirm_form', 'Подтверждение с обоснованием (комментарий записывается в решение)');
await clickText('.decision-form button', 'Сохранить');
await sleep(1800);
const afterConfirm = (await queueOf(tyuProcess)).items.find((i) => i.id === target.id);
const entry = (await api(`/api/case10/evidence-groups/${target.id}/protocol-entry`)).body;
const rowStatus = await evaluate("document.querySelector('.queue-row.selected')?.innerText.replace(/\\s+/g, ' ') || ''");
note('check5_status_consistency', { list_item: `${afterConfirm.finding_status}/${afterConfirm.inspector_status}`, queue_row: rowStatus.slice(0, 160),
  protocol_entry: JSON.stringify(entry).slice(0, 300) });
// the decided record leaves the "Кандидаты" queue: open it in "Решённые", then reload the page
const cardState = "({title: document.querySelector('.candidate-title')?.textContent, row: document.querySelector('.queue-row.selected')?.innerText.replace(/\\s+/g, ' ').slice(0,160)})";
await clickText('.queue-mode', 'Решённые');
await setFilter('142');
await clickText('.queue-row', 'M-078');
await waitFor("!!document.querySelector('.candidate-title')", 30000, 'decided card');
const beforeReload = await evaluate(cardState);
await navigate(`${APP}/`);
await waitFor("!!document.querySelector('.topbar')", 20000);
await clickText('.tab', 'Проверка');
await waitFor("!!document.querySelector('.candidate-title')", 60000, 'card after reload');
const afterReload = await evaluate(cardState);
note('check5_after_reload', { before: beforeReload, after: afterReload, same: beforeReload.title === afterReload.title });
await shot('07_after_reload', 'После обновления страницы: открыта та же запись, решение и обоснование сохранены');

// ================= Hypothesis -> candidate (never run in a browser before)
await boot(hyp.id);
const hypBefore = await queueOf(hypProcess);
const countBy = (items) => items.reduce((acc, i) => { const k = i.hypothesis ? 'hypothesis' : (i.evidence_tier || i.finding_status); acc[k] = (acc[k] || 0) + 1; return acc; }, {});
note('hyp_before', countBy(hypBefore.items));
await clickText('.queue-mode', 'Гипотезы');
await waitFor("!!document.querySelector('.queue-row')", 10000, 'hypothesis rows');
await clickText('.queue-row', 'FREE-SEARCH');
await waitFor("!!document.querySelector('.hypothesis-card')", 30000, 'hypothesis card');
note('hypothesis_card_text', await evaluate("document.querySelector('.hypothesis-card').innerText.slice(0, 1500)"));
await shot('08_hypothesis_card', 'Карточка гипотезы (ТЗ 9.5): метод, уверенность, описание, ссылки на ПД/РД, критичность, основание, статус рассмотрения');
await clickText('.hypothesis-card button', 'Преобразовать в кандидата');
await focusAndType('textarea[aria-label="Основание рассмотрения гипотезы"]', 'Расхождение площади видно на листах ПД и РД, оба места указаны; нужна проверка как нарушение');
await shot('09_hypothesis_to_candidate_form', '«Преобразовать в кандидата»: основание обязательно, источники с файлом, страницей и рамкой');
await clickText('.hypothesis-card button', 'Сохранить статус');
await waitFor("!document.querySelector('.hypothesis-card') || document.querySelector('.hypothesis-card').innerText.includes('Кандидат')", 30000, 'converted');
await sleep(1500);
const hypAfter = await queueOf(hypProcess);
note('hyp_after', countBy(hypAfter.items));
await shot('10_hypothesis_converted', 'После преобразования: запись стала кандидатом, в очереди гипотез на одну меньше, у кандидата — кнопки решения');

// ================= Novoslobodskaya: check 4 (incomplete package -> exact reason; supplement through the file picker)
await boot(nov.id);
const novBefore = await queueOf(novProcess);
const noDocs = novBefore.items.find((i) => i.no_result === 'NO_DOCUMENTS');
note('check4_no_result_state', { code: noDocs?.matrix_code, state: noDocs?.no_result, reason: noDocs?.reason });
await clickText('.queue-mode', 'Все');
await setFilter(noDocs.matrix_code);
await waitFor("!!document.querySelector('.queue-row')", 10000, 'queue row');
await clickText('.queue-row', noDocs.matrix_code);
await waitFor("!!document.querySelector('.no-result, .stage-panel.missing')", 30000, 'no-result block');
note('check4_reason_text', await evaluate("document.querySelector('.no-result')?.innerText || ''"));
await shot('11_incomplete_reason', 'Неполный комплект: точная причина (нет документов стадии) и доступное действие «Дозагрузить»');
await clickText('.no-result button, .stage-panel.missing button', 'Дозагрузить');
await waitFor("!!document.querySelector('.modal-box[aria-label=\"Дозагрузить документы\"]')", 5000, 'supplement dialog');
const { root } = await send('DOM.getDocument', { depth: 1 });
const { nodeId } = await send('DOM.querySelector', { nodeId: root.nodeId, selector: 'input[type=file]' });
await send('DOM.setFileInputFiles', { files: [resolve(args.supplement)], nodeId });
await waitFor("document.querySelectorAll('.modal-box li').length === 1", 5000, 'file listed');
await shot('12_supplement_file_chosen', '«Дозагрузить»: файл выбран через окно выбора файла');
await clickText('.modal-box button', 'Загрузить и пересчитать');
// the dialog closes itself when the recomputation settled (onDone); an error stays in the banner
let lastLog = '';
const started = Date.now();
while (Date.now() - started < 240000) {
  const state = await evaluate("({open: !!document.querySelector('.modal-box'), log: document.querySelector('.supplement-log')?.innerText || '', error: document.querySelector('.modal-box .error-banner')?.innerText || ''})");
  if (state.log) lastLog = state.log;
  if (state.error) { note('check4_supplement_error', state.error); break; }
  if (!state.open) break;
  await sleep(500);
}
note('check4_supplement_log', lastLog);
note('check4_supplement_seconds', Math.round((Date.now() - started) / 1000));
await sleep(1500);
await shot('13_supplement_result', 'После дозагрузки: окно закрылось, пересчёт затронутых параметров выполнен');
if (await evaluate("!!document.querySelector('.modal-box')")) await clickText('.modal-box button', 'Закрыть', { exact: true }).catch(() => {});
const novAfter = await queueOf(novProcess);
const before = snapshotOf(novBefore.items);
const after = snapshotOf(novAfter.items);
const changed = Object.keys(after).filter((id) => before[id] !== undefined && before[id] !== after[id]);
note('check4_cards_before_after', { before: novBefore.items.length, after: novAfter.items.length, changed: changed.length,
  new: Object.keys(after).filter((id) => before[id] === undefined).length, removed: Object.keys(before).filter((id) => after[id] === undefined).length,
  changed_ids_sample: changed.slice(0, 8) });
await shot('14_after_supplement_queue', 'Очередь после дозагрузки: изменившиеся карточки помечены, остальные не затронуты');

// ================= Altufyevskoye (package without ИД): check 6 (finalization warning, lock, history, protocol)
const alt = projectOf('экспликации помещений');
const altProcess = await processOf(alt.id);
await boot(alt.id);
const tyuNow = await queueOf(altProcess);
for (const item of tyuNow.items.filter((i) => i.finding_status === 'CANDIDATE' && i.inspector_status === 'PENDING')) {
  await api(`/api/case10/evidence-groups/${item.id}/decisions`, { method: 'POST', body: JSON.stringify({ decision: 'Confirm', comment: 'Подтверждено при проверке листов ПД/РД' }) });
}
await navigate(`${APP}/`);
await waitFor("!!document.querySelector('.topbar')", 20000);
await clickText('.tab', 'Проверка');
await waitFor("!!document.querySelector('.progress-panel')", 60000);
await top();
await waitFor("[...document.querySelectorAll('button')].some((b) => b.textContent.includes('Финализировать протокол'))", 60000, 'finalize button');
await clickText('button', 'Финализировать протокол');
await waitFor("!!document.querySelector('.finalize-warning')", 10000, 'ИД warning');
await shot('15_finalize_warning', 'Финализация: предупреждение «ИД не загружена»; завершить можно только с отметкой');
await clickText('.finalize-warning label', 'Я понимаю');
await clickText('.modal-box button', 'Финализировать', { exact: true });
await waitFor("!!document.querySelector('.finalize-banner')", 30000, 'finalized');
await sleep(800);
await shot('16_finalized_view_mode', 'После финализации: режим просмотра, решения заблокированы');
const protocol = (await api(`/api/case10/protocols/current?project_id=${alt.id}&process_id=${altProcess}`)).body;
const lockTarget = tyuNow.items.find((i) => i.finding_status === 'CANDIDATE').id;
const afterDecision = await api(`/api/case10/evidence-groups/${lockTarget}/decisions`, { method: 'POST', body: JSON.stringify({ decision: 'Reject', reason_code: 'OTHER', comment: 'попытка после финализации' }) });
note('check6_decision_after_finalize_status', afterDecision.status);
const unfinalize = await api(`/api/case10/protocols/${protocol.id}/unfinalize`, { method: 'POST', body: JSON.stringify({ reason: '' }) });
note('check6_unfinalize_without_reason_status', unfinalize.status);
const history = await api(`/api/case10/evidence-groups/${lockTarget}/protocol-entry`);
note('check6_history_entry', { status: history.status, keys: Object.keys(history.body || {}).slice(0, 14), text: JSON.stringify(history.body).slice(0, 400) });
const pdf = await fetch(`${API}/api/case10/protocols/${protocol.id}/pdf`, { headers: auth });
note('protocol_pdf', { status: pdf.status, type: pdf.headers.get('content-type'), bytes: (await pdf.arrayBuffer()).byteLength });
const submission = await api(`/api/case10/protocols/${protocol.id}/submission`);
const seen = {};
const exported = submission.body?.submission || submission.body || {};
for (const c of exported.checks || []) { const k = `${c.parameter_code}|${c.location}`; seen[k] = (seen[k] || 0) + 1; }
note('protocol_submission', { status: submission.status, checks: (exported.checks || []).length,
  duplicate_code_location_pairs: Object.entries(seen).filter(([, n]) => n > 1).map(([k, n]) => `${k} x${n}`).slice(0, 10) });

writeFileSync(join(OUT, 'checks.json'), JSON.stringify(checks, null, 1));
writeFileSync(join(OUT, 'shots.json'), JSON.stringify(shots, null, 1));
log('done', shots.length, 'screenshots ->', OUT);
ws.close();
chrome.kill();
process.exit(0);
