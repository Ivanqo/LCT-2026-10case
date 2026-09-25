#!/usr/bin/env node
// Phase 12 / S5: scripted end-to-end walkthrough of the inspector workbench with a screenshot of every step.
// No npm dependencies: drives a local Chromium over the DevTools protocol with Node's built-in WebSocket (Node >= 22).
//
//   node frontend_react/scripts/s5_walkthrough.mjs --out evaluation/phase12/s5_screens \
//        [--app http://localhost:5173] [--api http://127.0.0.1:8080] [--chrome <path to chrome.exe>]
//
// Expects the demo stack from evaluation/phase12/s5_demo_serve.py (fresh: --reset) and the Vite dev server.
// Demo credentials come from S5_DEMO_LOGIN / S5_DEMO_PASSWORD (local demo database only).
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir, homedir } from 'node:os';
import { join, resolve } from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, cur, i, all) => (cur.startsWith('--') ? [...acc, [cur.slice(2), all[i + 1]]] : acc), []));
const OUT = resolve(args.out || 'evaluation/phase12/s5_screens');
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
  throw new Error(`timeout waiting for ${what}`);
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
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });

for (let i = 0; ; i += 1) { // the gateway answers before the Python API has booted
  try {
    if ((await (await fetch(`${API}/health`)).json()).status === 'ok') break;
  } catch { /* not up yet */ }
  if (i > 120) throw new Error(`API behind ${API} is not healthy`);
  await sleep(500);
}
const login = await (await fetch(`${API}/api/auth/login`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ login: process.env.S5_DEMO_LOGIN || 'admin', password: process.env.S5_DEMO_PASSWORD || 'S5demo!2026' }),
})).json();
const token = login.token || login.access_token;
const auth = { Authorization: `Bearer ${token}` };
const projects = await (await fetch(`${API}/api/projects`, { headers: auth })).json();
const tyu = projects.find((p) => p.name.includes('Тюменская'));
const alt = projects.find((p) => p.name.includes('Алтуфьевское'));
const processOf = async (projectId) => (await (await fetch(`${API}/api/case10/processes?project_id=${projectId}`, { headers: auth })).json())[0].process_id;
const tyuProcess = await processOf(tyu.id);
const queue = await (await fetch(`${API}/api/case10/processes/${tyuProcess}/workbench`, { headers: auth })).json();
const pendingCandidates = queue.items.filter((i) => i.finding_status === 'CANDIDATE').length;
if (pendingCandidates !== 6) throw new Error(`demo DB is not fresh (${pendingCandidates} candidates); restart s5_demo_serve.py with --reset`);

await navigate(`${APP}/`);
await waitFor("!!document.querySelector('.auth-card')", 20000, 'login screen');
await shot('login', 'Вход (учётные данные демо-стенда; сессия для скрипта получена через API)');
await evaluate(`localStorage.setItem('case10_react_token', ${JSON.stringify(token)}); localStorage.setItem('case10_react_project_id', '${tyu.id}'); true`);
await navigate(`${APP}/`);
await waitFor("!!document.querySelector('.topbar')", 20000, 'app shell');

// 1. open the protocol -> the verification clock starts
await clickText('.tab', 'Проверка');
await waitFor("document.querySelector('.stage-panel img') && document.querySelector('.stage-panel img').complete", 40000, 'panels');
await shot('workbench', 'Проверка открыта: очередь кандидатов, карточка доказательства (§14), панель решения, запущен таймер верификации');
await scrollTo('.decision-bar', 'start');
await waitFor("[...document.querySelectorAll('.stage-panel img')].every((i) => i.complete)", 30000, 'panel images');
await shot('panels_pd_rd', 'Синхронные панели ПД/РД: файл, шифр, редакция, статус, страница, выделенный bbox, масштаб, «Источник ↗»; ИД-панель скрыта — ИД нет');

// 2. three conscious actions: open -> C -> Enter
const first = await heading();
await key('c');
await shot('decision_confirm_chosen', `«${first}»: выбрано «Подтвердить» (клавиша C), счётчик действий = 2`);
await key('Enter', { code: 'Enter', vk: 13, text: '\r' });
await waitFor(`document.querySelector('.candidate-title')?.textContent !== ${JSON.stringify(first)}`, 20000, 'auto-advance');
await waitFor("document.querySelector('.stage-panel img')?.complete", 30000);
await evaluate("document.querySelector('.content').scrollTo({top: 0}); true");
await shot('after_save_autoadvance', 'Enter сохранил решение (3 действия), открыт следующий кандидат; счётчики и таймер обновились');

// 3. reject needs a reason_code and a comment
await key('r');
await shot('reject_requires_comment', 'Отклонение (R): причина reason_code + обязательный комментарий, «Сохранить» недоступно без него');
await typeText('В РД ветви показаны на соседнем листе, система сохранена');
await key('Enter', { code: 'Enter', vk: 13, ctrl: true, text: '\r' });
await waitFor("(document.querySelector('.queue-mode.active .count')?.textContent || '') === '4'", 20000, 'reject saved');

// 4. bulk: same-type only, explicit confirmation for a mass rejection
await clickText('.queue-foot button', 'Отметить однотипные');
await clickText('.bulk-bar button', 'Отклонить все');
await waitFor("!!document.querySelector('.modal-box .confirm-check')");
await focusAndType('.modal-box textarea', 'По листам ОВ1 изм.4 системы сохранены');
await shot('bulk_reject_dialog', 'Групповое отклонение однотипных (M-078): общий комментарий, «Отклонить N» заблокировано до явного подтверждения');
await clickText('.modal-box button', 'Отмена', { exact: true });
if (await setChecked('M-079', true)) {
  await shot('bulk_mixed_type_guard', 'Смешанный выбор (M-078 + M-079): групповые операции заблокированы — только однотипные кандидаты');
  await setChecked('M-079', false);
}
// keep the current candidate out of the batch, clarify the others in one go
await setChecked(null, false);
await waitFor("!!document.querySelector('.bulk-bar') && !document.querySelector('.bulk-bar.invalid')", 5000, 'same-type selection');
await clickText('.bulk-bar button', 'Уточнить все');
await focusAndType('.modal-box textarea', 'Нужна сверка с аксонометрией: в РД изм.4 перераспределены ветви');
await clickText('.modal-box button', 'Применить к');
await waitFor("!document.querySelector('.modal-box')", 20000, 'bulk applied');

// 5. evidence editing: refine the РД box by drawing, add a ПД fragment, exclude and restore it
await scrollTo('.evidence-editor', 'start');
await clickText('.frag-table tr', 'фактическое');
const refineBtn = await evaluate("(() => { const row = [...document.querySelectorAll('.frag-table tbody tr')].find((r) => r.textContent.includes('фактическое')); const b = [...row.querySelectorAll('button')].find((x) => x.textContent === 'Уточнить'); b.scrollIntoView({block:'center'}); const r = b.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()");
await click(refineBtn[0], refineBtn[1]);
await clickText('.edit-form button', 'Выделить область');
await sleep(600); // draw mode may switch the render resolution: wait for the new page image
await waitFor("document.querySelector('.stage-panel.drawing img')?.complete", 30000, 'draw page');
await scrollTo('.stage-panel.drawing', 'center');
const vp = await evaluate("(() => { const r = document.querySelector('.stage-panel.drawing .stage-viewport').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()");
await drag([vp[0] + vp[2] * 0.35, vp[1] + vp[3] * 0.30], [vp[0] + vp[2] * 0.70, vp[1] + vp[3] * 0.62]);
await shot('refine_draw_bbox', 'Уточнение фрагмента РД: инспектор обводит новую область прямо на странице (зелёная рамка — черновик новой версии)');
await focusAndType('.edit-form input[placeholder*="рамка"]', 'Рамка захватывала соседнее помещение — сужена до зоны помещения');
await clickText('.edit-form button', 'Сохранить версию');
await waitFor("!document.querySelector('.edit-form')", 20000, 'refine saved');

await clickText('.editor-head button', 'Добавить фрагмент');
const pdDoc = await evaluate("(() => { const sel = document.querySelectorAll('.edit-form select')[1]; const opt = [...sel.options].find((o) => o.textContent.includes('ОВ')); return opt ? opt.value : sel.options[1].value; })()");
await evaluate(`(() => { const sel = document.querySelectorAll('.edit-form select')[1]; const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set; setter.call(sel, ${JSON.stringify(pdDoc)}); sel.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
const pdPage = await evaluate("(() => { const f = [...document.querySelectorAll('.frag-table tbody tr')].find((r) => r.textContent.includes('эталон')); const m = f && f.textContent.match(/с\\.(\\d+)/); return m ? m[1] : '1'; })()");
await evaluate(`(() => { const input = document.querySelector('.edit-form input[type=number]'); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; setter.call(input, ${JSON.stringify(pdPage)}); input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
await focusAndType('.edit-form .field.wide input', 'Подпись системы у помещения на схеме');
await clickText('.edit-form button', 'Выделить область');
await sleep(600);
await waitFor("document.querySelector('.stage-panel.drawing img')?.complete", 30000, 'draw page');
await scrollTo('.stage-panel.drawing', 'center');
const vp2 = await evaluate("(() => { const r = document.querySelector('.stage-panel.drawing .stage-viewport').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()");
await drag([vp2[0] + vp2[2] * 0.40, vp2[1] + vp2[3] * 0.25], [vp2[0] + vp2[2] * 0.62, vp2[1] + vp2[3] * 0.40]);
await focusAndType('.edit-form input[placeholder*="рамка"]', 'В ПД система подписана ещё и на схеме — второе доказательство');
await clickText('.edit-form button', 'Сохранить версию');
await waitFor("!document.querySelector('.edit-form')", 20000, 'add saved');
const excludeManual = await evaluate("(() => { const row = [...document.querySelectorAll('.frag-table tbody tr')].find((r) => r.textContent.includes('инспектор')); const b = [...row.querySelectorAll('button')].find((x) => x.textContent === 'Исключить'); b.scrollIntoView({block:'center'}); const r = b.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()");
await click(excludeManual[0], excludeManual[1]);
await typeText('Дублирует машинный фрагмент ПД на той же странице');
await clickText('.edit-form button', 'Исключить (новая версия)');
await waitFor("!document.querySelector('.edit-form')", 20000, 'exclude saved');
await clickText('.editor-head label', 'показывать исключённые');
await clickText('.frag-table button', 'Восстановить');
await typeText('Проверено: другой фрагмент схемы — возвращён');
await clickText('.edit-form button', 'Восстановить (новая версия)');
await waitFor("!document.querySelector('.edit-form')", 20000, 'restore saved');
await scrollTo('.evidence-editor', 'start');
await shot('evidence_versions_history', 'Доказательства: уточнён машинный фрагмент (v1), добавлен/исключён/восстановлен фрагмент инспектора (v1–v3); история: пользователь, время, причина, прежнее значение, ссылка на исходный фрагмент');
await scrollTo('.stage-panels', 'start');
await shot('panels_after_edits', 'Панели после правок: красная рамка — уточнённая область РД, зелёная пунктирная — фрагмент, добавленный инспектором');

// 6. low-confidence / LOW_QUALITY queue
await evaluate("document.querySelector('.content').scrollTo({top: 0}); true");
await clickText('.queue-mode', 'Низкая уверенность');
await sleep(800);
await shot('lowconf_queue', 'Очередь low-confidence / LOW_QUALITY с настраиваемым порогом, фильтры по разделам и статусам');
await clickText('.queue-mode', 'Кандидаты');

// 7. finish the queue with the keyboard and finalize -> measured verification time
for (let i = 0; i < 6; i += 1) {
  const left = await evaluate("document.querySelectorAll('.queue-row').length");
  if (!left) break;
  await key('c');
  await key('Enter', { code: 'Enter', vk: 13, text: '\r' });
  await sleep(2500);
}
await evaluate("document.querySelector('.content').scrollTo({top: 0}); true");
await clickText('.btn', 'Финализировать протокол');
await clickText('.modal-box button', 'Финализировать', { exact: true });
await waitFor("(document.querySelector('.verification-timer .t-label')?.textContent || '').includes('Время верификации')", 30000, 'finalized');
await shot('finalized_verification_time', 'Протокол финализирован: время верификации «от открытия протокола до финализации» зафиксировано в журнале');

// 8. revisions and completeness on the second demo object (edition conflicts)
await evaluate(`localStorage.setItem('case10_react_project_id', '${alt.id}'); true`);
await navigate(`${APP}/`);
await waitFor("!!document.querySelector('.topbar')");
await clickText('.tab', 'Проверка');
await waitFor("!!document.querySelector('.subtabs')", 30000);
await clickText('.subtab', 'Редакции');
await waitFor("!!document.querySelector('.revision-scope')", 30000, 'revision scopes');
await shot('revisions_conflicts', 'Редакции: противоречивые признаки → CLARIFICATION_REQUIRED с основанием, без молчаливого выбора');
await clickText('.revision-scope button', 'Выбрать актуальной');
await typeText('По реестру заказчика действует П-2025-04-266 Изм. 1, серия ЖС-270121 — прежняя');
await shot('revision_choice_dialog', 'Выбор редакции инспектором: обоснование обязательно');
await clickText('.modal-box button', 'Сохранить выбор');
await waitFor("!document.querySelector('.modal-box')", 20000);
await evaluate("document.querySelector('.revision-scope details')?.setAttribute('open', ''); document.querySelector('.content').scrollTo({top: 0}); true");
await scrollTo('.subtabs', 'start');
await shot('revision_chosen_history', 'Редакция выбрана инспектором: новая версия с историей (прежнее значение — «не определено системой»)');
await clickText('.subtab', 'Комплектность');
await waitFor("!!document.querySelector('.plain-table')", 30000, 'completeness');
await scrollTo('.subtabs', 'start');
await shot('completeness', 'Комплектность против ожидаемого манифеста: загружено / ожидалось / основание / неопределённость');

// journal evidence for the report
const audit = await (await fetch(`${API}/api/case10/processes/${tyuProcess}/audit`, { headers: auth })).json();
const keep = new Set(['VERIFICATION_OPENED', 'DECISION_UI_METRICS', 'BULK_DECISION', 'EVIDENCE_FRAGMENT_ADDED', 'EVIDENCE_FRAGMENT_REFINED',
  'EVIDENCE_FRAGMENT_REMOVED', 'EVIDENCE_FRAGMENT_RESTORED', 'PROTOCOL_FINALIZED', 'VERIFICATION_TIME_MEASURED']);
writeFileSync(join(OUT, 'audit_excerpt.json'), JSON.stringify(audit.filter((r) => keep.has(r.action)), null, 1));
writeFileSync(join(OUT, 'shots.json'), JSON.stringify(shots, null, 1));
log('done', shots.length, 'screenshots ->', OUT);
ws.close();
chrome.kill();
process.exit(0);
