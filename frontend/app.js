(function(){
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const API_BASE = (() => {
    const qs = new URLSearchParams(window.location.search);
    if (qs.get('api')) return qs.get('api').replace(/\/$/, '');
    if (window.location.port && window.location.port !== '8000') return `${window.location.protocol}//${window.location.hostname}:8000`;
    return `${window.location.protocol}//${window.location.host}`;
  })();

  const TOKEN_KEY = 'pd_rdmvp_access_token_v2';
  const REGION_LABELS = {
    table: 'Таблица',
    drawing: 'Чертёж / схема',
    page: 'Лист',
    title_block: 'Основная надпись',
    text: 'Текстовый блок',
  };
  const FINDING_STATUS_LABELS = {
    CANDIDATE: 'Кандидат',
    CONFIRMED_VIOLATION: 'Подтверждено',
    NEGATIVE_VERIFIED: 'Проверено, нарушения нет',
    MISSING_EVIDENCE: 'Нет доказательств',
    NOT_APPLICABLE: 'Не применимо',
    NOT_COMPARABLE: 'Не сопоставимо',
    CLARIFICATION_REQUIRED: 'Нужно уточнение',
    SUSPICION: 'Гипотеза',
  };
  const PROCESS_STATUS_LABELS = {
    PENDING: 'Ожидает',
    PARSING: 'Разбор документов',
    READY: 'Готово к проверке',
    VERIFYING: 'Инспектор проверяет',
    COMPLETED: 'Проверено',
    FINALIZED: 'Финализировано',
  };

  let me = null;
  let currentProjectId = null;
  let chatState = [];
  let sendInFlight = false;
  let docsPollTimer = null;
  let chatJobPollTimer = null;
  let selectedEntityId = null;
  const protectedImageCache = new Map();
  const protectedPageImageCache = new Map();

  function injectMarkdownStyles(){
    if(document.getElementById('simpleMvpMarkdownStyles')) return;
    const style = document.createElement('style');
    style.id = 'simpleMvpMarkdownStyles';
    style.textContent = `
      .rich-text h1,.rich-text h2,.rich-text h3{margin:14px 0 8px;line-height:1.25}.rich-text p{margin:8px 0;line-height:1.55}.rich-text ul,.rich-text ol{margin:8px 0 10px 22px}.rich-text li{margin:5px 0;line-height:1.5}.rich-text blockquote{margin:10px 0;padding:10px 12px;border-left:3px solid currentColor;opacity:.85;background:rgba(127,127,127,.08);border-radius:10px}.rich-text code{padding:2px 5px;border-radius:6px;background:rgba(127,127,127,.14)}.source-ref{display:inline-flex;align-items:center;gap:4px;margin:1px 2px;padding:2px 8px;border-radius:999px;background:rgba(59,130,246,.12);border:1px solid rgba(59,130,246,.22);font-size:.92em}.md-table-wrap{overflow:auto;margin:10px 0;border-radius:12px;border:1px solid rgba(127,127,127,.18)}.md-table{width:100%;border-collapse:collapse}.md-table th,.md-table td{padding:8px 10px;border-bottom:1px solid rgba(127,127,127,.16);vertical-align:top}.md-table th{text-align:left;background:rgba(127,127,127,.08)}
    `;
    document.head.appendChild(style);
  }

  function api(path){ return `${API_BASE}${path}`; }
  function token(){ return localStorage.getItem(TOKEN_KEY) || ''; }
  function setToken(v){ if(v) localStorage.setItem(TOKEN_KEY, v); else localStorage.removeItem(TOKEN_KEY); }
  function authHeaders(extra){ return Object.assign({'Content-Type':'application/json', 'Authorization': `Bearer ${token()}`}, extra || {}); }
  function setStatus(text, kind='ok'){ const el=$('#statusBox'); if(!el) return; el.textContent=text; el.className=`status ${kind}`; }
  function showError(err){ const el=$('#errorBox'); if(!el) return; el.textContent=typeof err==='string'?err:(err?.message||String(err)); el.classList.remove('hidden'); }
  function clearError(){ const el=$('#errorBox'); if(!el) return; el.classList.add('hidden'); el.textContent=''; }
  function setChatProcessing(active, text){
    const wrap = $('#chatProcessing');
    const label = $('#chatProcessingText');
    if(!wrap) return;
    if(label && text) label.textContent = text;
    wrap.classList.toggle('hidden', !active);
    const chat = $('#chatMessages');
    if(chat) chat.scrollTop = chat.scrollHeight;
  }
  function showLoginError(text){ const el=$('#loginError'); el.textContent=text; el.classList.remove('hidden'); }
  function clearLoginError(){ const el=$('#loginError'); el.classList.add('hidden'); el.textContent=''; }
  function escapeHtml(s){ return String(s||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'); }
  function fmtDate(v){ if(!v) return ''; const d = new Date(v); return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString(); }
  function chatStorageKey(){
    return currentProjectId ? `pd_rdmvp_chat_state_${currentProjectId}` : 'pd_rdmvp_chat_state_no_project';
  }

  function saveChatState(){
    try{
      if(!currentProjectId) return;
      sessionStorage.setItem(chatStorageKey(), JSON.stringify(chatState || []));
    }catch(_e){}
  }

  function restoreChatState(){
    try{
      if(!currentProjectId){ chatState = []; return; }
      const raw = sessionStorage.getItem(chatStorageKey());
      const parsed = raw ? JSON.parse(raw) : [];
      chatState = Array.isArray(parsed) ? parsed : [];
    }catch(_e){
      chatState = [];
    }
  }

  function clearSavedChatState(){
    try{
      if(currentProjectId) sessionStorage.removeItem(chatStorageKey());
    }catch(_e){}
  }

  function pendingChatStorageKey(){
    return currentProjectId ? `pd_rdmvp_pending_chat_${currentProjectId}` : 'pd_rdmvp_pending_chat_no_project';
  }

  function hasAnyPendingChat(){
    try{
      for(let i = 0; i < sessionStorage.length; i += 1){
        const key = sessionStorage.key(i) || '';
        if(key.startsWith('pd_rdmvp_pending_chat_') && sessionStorage.getItem(key)) return true;
      }
    }catch(_e){}
    return false;
  }

  function rememberPendingChatGlobally(value){
    try{
      if(value) sessionStorage.setItem('pd_rdmvp_pending_chat_last', JSON.stringify(value));
      else sessionStorage.removeItem('pd_rdmvp_pending_chat_last');
    }catch(_e){}
  }

  function getLastPendingChat(){
    try{
      const raw = sessionStorage.getItem('pd_rdmvp_pending_chat_last');
      return raw ? JSON.parse(raw) : null;
    }catch(_e){ return null; }
  }

  function setPendingChat(value){
    try{
      if(value){
        if(currentProjectId) sessionStorage.setItem(pendingChatStorageKey(), JSON.stringify(value));
        rememberPendingChatGlobally(value);
      } else {
        if(currentProjectId) sessionStorage.removeItem(pendingChatStorageKey());
        rememberPendingChatGlobally(null);
      }
    }catch(_e){}
  }

  function getPendingChat(){
    try{
      if(currentProjectId){
        const raw = sessionStorage.getItem(pendingChatStorageKey());
        if(raw) return JSON.parse(raw);
      }
      const last = getLastPendingChat();
      if(last && (!currentProjectId || Number(last.project_id) === Number(currentProjectId))) return last;
      return null;
    }catch(_e){ return null; }
  }

  function hasUserMessage(question){
    const target = String(question || '').trim();
    return chatState.some(m => m.role === 'user' && String(m.text || '').trim() === target);
  }

  function clearChatJobPoll(){
    if(chatJobPollTimer){
      clearTimeout(chatJobPollTimer);
      chatJobPollTimer = null;
    }
  }

  function setQuestionControlsDisabled(disabled){
    const input = $('#questionInput');
    const sendBtn = $('#sendBtn');
    if(input) input.disabled = !!disabled;
    if(sendBtn) sendBtn.disabled = !!disabled;
  }

  function clipText(text, limit=260){ const clean = String(text||'').replace(/\s+/g,' ').trim(); return clean.length <= limit ? clean : `${clean.slice(0, limit - 1).trim()}…`; }
  function regionLabel(type){ return REGION_LABELS[type] || String(type || 'Материал'); }
  function buildRegionImageUrl(regionId){ return api(`/api/rag/region-image?region_id=${encodeURIComponent(regionId)}`); }
  function buildPageImageUrl(pageId){ return api(`/api/rag/page-image?page_id=${encodeURIComponent(pageId)}`); }
  function buildPageDownloadUrl(pageId){ return api(`/api/rag/page-download?page_id=${encodeURIComponent(pageId)}`); }
  function buildPagesPdfUrl(pageIds){ return api(`/api/rag/pages-pdf?page_ids=${encodeURIComponent((pageIds || []).join(','))}`); }
  function buildDocumentDownloadUrl(documentId, projectId, sourceType){ return api(`/api/documents/${encodeURIComponent(documentId)}/download?project_id=${encodeURIComponent(projectId)}&source_type=${encodeURIComponent(sourceType || 'rag')}`); }

  async function resolveProtectedImage(regionId){
    const key = String(regionId);
    if(protectedImageCache.has(key)) return protectedImageCache.get(key);
    const promise = fetch(buildRegionImageUrl(regionId), {headers:{'Authorization': `Bearer ${token()}`}, cache:'no-store'})
      .then(async (res) => {
        if(!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        return URL.createObjectURL(blob);
      })
      .catch(() => '');
    protectedImageCache.set(key, promise);
    return promise;
  }

  async function resolveProtectedPageImage(pageId){
    const key = String(pageId);
    if(protectedPageImageCache.has(key)) return protectedPageImageCache.get(key);
    const promise = fetch(buildPageImageUrl(pageId), {headers:{'Authorization': `Bearer ${token()}`}, cache:'no-store'})
      .then(async (res) => {
        if(!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        return URL.createObjectURL(blob);
      })
      .catch(() => '');
    protectedPageImageCache.set(key, promise);
    return promise;
  }

  async function hydrateProtectedImages(root){
    const imgs = Array.from((root || document).querySelectorAll('img.protected-region-image[data-region-id]'));
    await Promise.all(imgs.map(async (img) => {
      if(img.dataset.loaded === '1') return;
      img.dataset.loaded = '1';
      const src = await resolveProtectedImage(img.dataset.regionId);
      if(src) img.src = src;
      else img.closest('.asset-preview, .source-asset-chip, .evidence-card')?.classList.add('image-failed');
    }));
  }

  async function hydrateProtectedPageImages(root){
    const imgs = Array.from((root || document).querySelectorAll('img.protected-page-image[data-page-id]'));
    await Promise.all(imgs.map(async (img) => {
      if(img.dataset.loaded === '1') return;
      img.dataset.loaded = '1';
      const src = await resolveProtectedPageImage(img.dataset.pageId);
      if(src) img.src = src;
      else img.closest('.pdf-viewer-stage, .pdf-viewer')?.classList.add('image-failed');
    }));
  }

  function closeImageLightbox(){
    const root = $('#imageLightbox');
    if(!root) return;
    root.classList.add('hidden');
    root.setAttribute('aria-hidden', 'true');
    $('#lightboxImage').removeAttribute('src');
    $('#lightboxCaption').textContent = '';
    document.body.classList.remove('modal-open');
  }

  function openImageLightbox({src, caption}){
    if(!src) return;
    const root = $('#imageLightbox');
    if(!root) return;
    $('#lightboxImage').src = src;
    $('#lightboxCaption').textContent = caption || '';
    root.classList.remove('hidden');
    root.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
  }


  function uploadStatusLabel(status){
    if(status === 'queued') return 'В очереди';
    if(status === 'processing') return 'Обрабатывается';
    if(status === 'ready') return 'Готов';
    if(status === 'failed') return 'Ошибка';
    return status || 'Неизвестно';
  }

  function startDocsPolling(){
    stopDocsPolling();
    docsPollTimer = window.setInterval(() => {
      if(currentProjectId && !document.hidden && !$('#docsView')?.classList.contains('hidden')){
        refreshDocs({silent:true}).catch(() => {});
      }
    }, 2500);
  }

  function stopDocsPolling(){
    if(docsPollTimer){
      window.clearInterval(docsPollTimer);
      docsPollTimer = null;
    }
  }

  function normalizeRefs(raw){
    if(Array.isArray(raw)){
      return { selected_chunk_ids: [], selected_asset_ids: [], pages: [], sources: raw, assets: [] };
    }
    if(raw && typeof raw === 'object'){
      return {
        selected_chunk_ids: Array.isArray(raw.selected_chunk_ids) ? raw.selected_chunk_ids.map(Number).filter(Number.isFinite) : [],
        selected_asset_ids: Array.isArray(raw.selected_asset_ids) ? raw.selected_asset_ids.map(Number).filter(Number.isFinite) : [],
        pages: Array.isArray(raw.pages) ? raw.pages : [],
        sources: Array.isArray(raw.sources) ? raw.sources : [],
        assets: Array.isArray(raw.assets) ? raw.assets : [],
      };
    }
    return { selected_chunk_ids: [], selected_asset_ids: [], pages: [], sources: [], assets: [] };
  }

  function inlineMarkdown(text){
    return escapeHtml(text || '')
      .replace(/\[\[\s*source\s*:\s*([^,\]]+?)\s*(?:,\s*page\s*:\s*(\d+))?\s*\]\]/gi, (_m, file, page) => {
        const label = page ? `${file.trim()} • стр. ${page}` : file.trim();
        return `<span class="source-ref">${escapeHtml(label)}</span>`;
      })
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_]+)__/g, '<strong>$1</strong>')
      .replace(/\*([^*]+)\*/g, '<em>$1</em>');
  }

  function renderMarkdownTable(lines, startIndex){
    const header = lines[startIndex].trim();
    const sep = lines[startIndex + 1]?.trim() || '';
    if(!/^\|.*\|$/.test(header) || !/^\|?[\s:|\-]+\|?$/.test(sep)) return null;
    const split = (line) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(x => x.trim());
    const headers = split(header);
    const rows = [];
    let i = startIndex + 2;
    while(i < lines.length && /^\|.*\|$/.test(lines[i].trim())){
      rows.push(split(lines[i]));
      i += 1;
    }
    const headHtml = headers.map(h => `<th>${inlineMarkdown(h)}</th>`).join('');
    const bodyHtml = rows.map(row => `<tr>${headers.map((_h, idx) => `<td>${inlineMarkdown(row[idx] || '')}</td>`).join('')}</tr>`).join('');
    return { html: `<div class="md-table-wrap"><table class="md-table"><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`, nextIndex: i };
  }

  function renderRichText(text){
    const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
    const html = [];
    let listType = null;
    const closeList = () => { if(listType){ html.push(`</${listType}>`); listType = null; } };
    const openList = (type) => { if(listType !== type){ closeList(); html.push(`<${type}>`); listType = type; } };

    for(let i = 0; i < lines.length; i += 1){
      const rawLine = lines[i];
      const line = rawLine.trimEnd();
      const trimmed = line.trim();
      if(!trimmed){ closeList(); continue; }

      const table = renderMarkdownTable(lines, i);
      if(table){ closeList(); html.push(table.html); i = table.nextIndex - 1; continue; }

      if(/^---+$/.test(trimmed)){ closeList(); html.push('<hr>'); continue; }
      if(/^###\s+/.test(trimmed)){ closeList(); html.push(`<h3>${inlineMarkdown(trimmed.replace(/^###\s+/, ''))}</h3>`); continue; }
      if(/^##\s+/.test(trimmed)){ closeList(); html.push(`<h2>${inlineMarkdown(trimmed.replace(/^##\s+/, ''))}</h2>`); continue; }
      if(/^#\s+/.test(trimmed)){ closeList(); html.push(`<h1 class="rich-inline-h1">${inlineMarkdown(trimmed.replace(/^#\s+/, ''))}</h1>`); continue; }
      if(/^>\s?/.test(trimmed)){ closeList(); html.push(`<blockquote>${inlineMarkdown(trimmed.replace(/^>\s?/, ''))}</blockquote>`); continue; }
      if(/^(?:[-*•])\s+/.test(trimmed)){
        openList('ul');
        html.push(`<li>${inlineMarkdown(trimmed.replace(/^(?:[-*•])\s+/, ''))}</li>`);
        continue;
      }
      if(/^\d+[.)]\s+/.test(trimmed)){
        openList('ol');
        html.push(`<li>${inlineMarkdown(trimmed.replace(/^\d+[.)]\s+/, ''))}</li>`);
        continue;
      }
      closeList();
      html.push(`<p>${inlineMarkdown(trimmed)}</p>`);
    }
    closeList();
    return html.join('') || `<p>${inlineMarkdown(text || '')}</p>`;
  }

  async function fetchJSON(path, opts){
    const finalOpts = Object.assign({cache:'no-store'}, opts || {});
    let res;
    try{
      res = await fetch(api(path), finalOpts);
    }catch(e){
      throw new Error(`Не удалось выполнить запрос к API ${API_BASE}${path}. Backend недоступен, соединение оборвано или CORS заблокировал ответ. Детали: ${e.message || e}`);
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch(e) {}
    if(!res.ok){ throw new Error(data?.detail || data?.error?.message || text || `HTTP ${res.status}`); }
    return data;
  }

  async function downloadProtectedFile(url, suggestedName){
    const res = await fetch(url, {headers:{'Authorization': `Bearer ${token()}`}, cache:'no-store'});
    if(!res.ok) throw new Error(`Не удалось скачать файл (HTTP ${res.status})`);
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const headerName = res.headers.get('Content-Disposition') || '';
    const match = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(headerName);
    const filename = decodeURIComponent(match?.[1] || match?.[2] || suggestedName || 'document');
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
  function makeAccountsTxt(rows, title='Созданные аккаунты'){
    const lines = [title, '='.repeat(title.length), ''];
    (rows || []).forEach((r, idx) => {
      lines.push(`${idx + 1}. ${r.login || ''}`);
      lines.push(`   login: ${r.login || ''}`);
      lines.push(`   password: ${r.password || 'недоступен'}`);
      lines.push(`   email: ${r.email || ''}`);
      lines.push(`   api_key: ${r.api_key || ''}`);
      lines.push(`   role: ${r.is_admin ? 'admin' : 'user'}`);
      lines.push(`   organization_id: ${r.organization_id || ''}`);
      lines.push('');
    });
    return lines.join('\n');
  }

  function downloadTextFile(text, filename){
    const blob = new Blob([text || ''], {type:'text/plain;charset=utf-8'});
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = filename || 'accounts.txt';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }

  async function login(){
    clearLoginError();
    try{
      currentProjectId = null;
      chatState = [];
      const data = await fetchJSON('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({login: $('#loginInput').value.trim(), password: $('#passwordInput').value})});
      setToken(data.access_token);
      me = data.user;
      await initApp();
    }catch(e){ showLoginError(e.message || 'Ошибка входа'); }
  }

  async function logout(){
    try{ if(token()) await fetchJSON('/api/auth/logout', {method:'POST', headers: authHeaders()}); }catch(e){}
    setToken('');
    me = null;
    currentProjectId = null;
    chatState = [];
    $('#appShell').classList.add('hidden');
    $('#loginScreen').classList.remove('hidden');
  }

  async function loadMe(){
    me = await fetchJSON('/api/me', {headers: authHeaders()});
    const orgName = me.organization_name || 'Без организации';
    $('#meInfo').textContent = `${me.login} • ${orgName}${me.is_admin ? ' • администратор' : ''}`;
    document.body.classList.toggle('is-admin', !!me.is_admin);
    document.body.classList.toggle('is-user', !me.is_admin);
    $('#adminNavBtn').classList.toggle('hidden', !me.is_admin);
    const brandSub = $('#brandSub');
    if(brandSub) brandSub.textContent = me.is_admin ? 'Администрирование и объектный контроль' : 'Объекты, изменения, доказательства';
    document.querySelectorAll('.admin-only-option').forEach((el) => { el.disabled = !me.is_admin; el.hidden = !me.is_admin; });
  }

  function setView(id){
    $$('.view').forEach(v => v.classList.add('hidden'));
    $('#' + id).classList.remove('hidden');
    $$('.nav-item').forEach(v => v.classList.toggle('active', v.dataset.view === id));
  }

  async function refreshProjects(){
    const rows = await fetchJSON('/api/projects', {headers: authHeaders()});
    const sel = $('#projectSelect');
    sel.innerHTML='';
    const ids = rows.map(p => Number(p.id));
    rows.forEach((p) => {
      const o = document.createElement('option');
      o.value = p.id;
      o.textContent = p.name;
      sel.appendChild(o);
    });
    if(!rows.length){
      currentProjectId = null;
      return;
    }
    if(!Number.isFinite(Number(currentProjectId)) || !ids.includes(Number(currentProjectId))){
      currentProjectId = rows[0].id;
    }
    sel.value = String(currentProjectId);
  }

  async function createProject(){
    await fetchJSON('/api/projects', {method:'POST', headers: authHeaders(), body: JSON.stringify({name: $('#projectName').value.trim(), description: $('#projectDesc').value.trim() || null})});
    $('#projectName').value=''; $('#projectDesc').value='';
    await refreshProjects();
    await refreshCase10Shell();
    await refreshDocs();
    setStatus('Проект создан');
  }

  function stageLabel(stage){
    return ({project:'П', working:'Р', as_built:'И', ifc:'IFC', unknown:'Не указано'})[stage] || stage || 'Не указано';
  }

  function formatValueWithUnit(value, unit){
    const text = String(value ?? '').trim();
    const unitText = String(unit || '').trim();
    if(!text || !unitText) return text;
    return text.toLowerCase().endsWith(` ${unitText.toLowerCase()}`) ? text : `${text} ${unitText}`;
  }

  function riskClass(severity){
    const value = String(severity || '').toLowerCase();
    if(value === 'critical') return 'risk-critical';
    if(value === 'warning') return 'risk-warning';
    if(value === 'needs_review') return 'risk-review';
    return 'risk-low';
  }

  function findingStatusLabel(status){
    return FINDING_STATUS_LABELS[status] || status || 'Неизвестно';
  }

  function processStatusLabel(status){
    return PROCESS_STATUS_LABELS[status] || status || 'Не запускался';
  }

  function findingClass(status){
    const value = String(status || '').toLowerCase();
    if(value === 'candidate' || value === 'confirmed_violation') return 'risk-critical';
    if(value === 'clarification_required' || value === 'missing_evidence' || value === 'not_comparable') return 'risk-warning';
    if(value === 'suspicion') return 'risk-review';
    return 'risk-low';
  }

  function fragmentRoleLabel(role){
    if(role === 'expected') return 'Expected';
    if(role === 'actual') return 'Actual';
    return 'Context';
  }

  async function refreshCase10Shell(){
    if(!currentProjectId) return;
    await Promise.allSettled([
      refreshOverview(),
      refreshDocumentVersions(),
      refreshMatrix(),
      refreshCandidates(),
      refreshObjects({skipPortrait:false}),
      refreshChanges(),
      refreshProtocol(),
    ]);
  }

  async function seedSyntheticDataset(){
    if(!currentProjectId) throw new Error('Сначала выберите проект');
    setStatus('Создаю synthetic dataset…', 'warn');
    const result = await fetchJSON(`/api/case10/projects/${currentProjectId}/synthetic-dataset`, {method:'POST', headers: authHeaders()});
    selectedEntityId = result.canonical_entity_id;
    await refreshCase10Shell();
    setView('candidatesView');
    setStatus(result.created ? `Synthetic dataset создан, process_id ${result.process_id}` : `Synthetic dataset уже был создан, process_id ${result.process_id}`);
  }

  async function importOfficialDataset(){
    if(!currentProjectId) throw new Error('Сначала выберите проект');
    setStatus('Импортирую официальный CASE10 dataset…', 'warn');
    const result = await fetchJSON(`/api/case10/projects/${currentProjectId}/official-dataset/import`, {
      method:'POST',
      headers: authHeaders(),
      body: JSON.stringify({
        object_ids:['OBJ-TYUMENSKAYA-5-GOLD-SEED','OBJ-NOVOSLOBODSKAYA'],
        include_hidden:false,
        include_pages:true,
        include_annotations:true,
        include_gold:true,
        run_processes:true,
      }),
    });
    await refreshCase10Shell();
    setView('candidatesView');
    const processCount = (result.processes || []).length;
    const docs = result.files_index || result.document_manifest || {};
    setStatus(`Official dataset: ${result.matrix_params || 0} параметров, документов +${docs.created || 0}/${docs.updated || 0}, процессов ${processCount}`);
  }

  async function startInspection(){
    if(!currentProjectId) throw new Error('Сначала выберите проект');
    setStatus('Запускаю V3-проверку…', 'warn');
    const result = await fetchJSON(`/api/case10/projects/${currentProjectId}/processes`, {
      method:'POST',
      headers: authHeaders(),
      body: JSON.stringify({run_immediately:true}),
    });
    await refreshCase10Shell();
    setView('candidatesView');
    setStatus(`Проверка ${result.process_id} получила статус ${processStatusLabel(result.status)}`);
  }

  async function refreshOverview(){
    if(!currentProjectId) return;
    const data = await fetchJSON(`/api/case10/overview?project_id=${currentProjectId}`, {headers: authHeaders()});
    const stages = $('#overviewStages');
    if(stages){
      stages.innerHTML = (data.documents?.by_stage || []).map((item) => `
        <div class="stage-card">
          <span>${escapeHtml(item.label)}</span>
          <strong>${escapeHtml(String(item.count || 0))}</strong>
        </div>
      `).join('') || '<div class="muted">Версии документов пока не созданы.</div>';
    }
    const objects = $('#overviewObjects');
    if(objects) objects.textContent = String(data.objects?.total || 0);
    const changes = data.findings || {};
    const risk = $('#overviewChanges');
    if(risk){
      risk.innerHTML = `
        <div class="risk-line"><span class="risk-dot risk-critical"></span><span>CANDIDATE</span><strong>${escapeHtml(String(changes.CANDIDATE || 0))}</strong></div>
        <div class="risk-line"><span class="risk-dot risk-low"></span><span>NEGATIVE</span><strong>${escapeHtml(String(changes.NEGATIVE_VERIFIED || 0))}</strong></div>
        <div class="risk-line"><span class="risk-dot risk-warning"></span><span>CLARIFY / MISSING</span><strong>${escapeHtml(String((changes.CLARIFICATION_REQUIRED || 0) + (changes.MISSING_EVIDENCE || 0)))}</strong></div>
      `;
    }
    const inspection = $('#overviewInspection');
    if(inspection){
      const current = data.inspection;
      if(!current){
        inspection.classList.add('muted');
        inspection.innerHTML = 'Проверка еще не запускалась.';
      } else {
        inspection.classList.remove('muted');
        inspection.innerHTML = `
          <div class="inspection-grid">
            <div><span>Process</span><strong>${escapeHtml(current.process_id)}</strong></div>
            <div><span>Status</span><strong>${escapeHtml(processStatusLabel(current.status))}</strong></div>
            <div><span>Scenario</span><strong>${escapeHtml(current.upload_scenario || '')}</strong></div>
            <div><span>Protocol</span><strong>${current.protocol ? `v${escapeHtml(String(current.protocol.version))}` : 'нет'}</strong></div>
          </div>
        `;
      }
    }
    const flow = $('#pipelineFlow');
    if(flow){
      flow.innerHTML = (data.pipeline || []).map((step) => `<span>${escapeHtml(step)}</span>`).join('');
    }
  }

  async function refreshDocumentVersions(){
    if(!currentProjectId || !$('#docStageGroups')) return;
    const data = await fetchJSON(`/api/case10/documents?project_id=${currentProjectId}`, {headers: authHeaders()});
    const root = $('#docStageGroups');
    const groups = data.groups || [];
    if(!groups.length){
      root.innerHTML = '<div class="muted">DocumentVersion пока нет. Они появятся после обработки upload или synthetic dataset.</div>';
      return;
    }
    root.innerHTML = groups.map((group) => `
      <article class="doc-stage">
        <div class="doc-stage-head">
          <strong>${escapeHtml(group.stage_label || stageLabel(group.stage))}</strong>
          <span class="pill">${group.documents.length}</span>
        </div>
        <div class="doc-stage-list">
          ${group.documents.map((doc) => `
            <div class="doc-version-row">
              <div>
                <strong>${escapeHtml(doc.filename)}</strong>
                <div class="muted small">${escapeHtml(doc.document_code || 'document_code позже')} • ${escapeHtml(doc.discipline || 'discipline позже')} • rev ${escapeHtml(doc.revision || 'revision позже')}</div>
                <div class="muted small top-gap">${escapeHtml(doc.approval_status || 'UNKNOWN')} ${doc.approval_date ? `• ${fmtDate(doc.approval_date)}` : ''} ${doc.file_hash ? `• SHA-256 ${escapeHtml(String(doc.file_hash).slice(0, 12))}` : ''}</div>
                <div class="muted small top-gap">${doc.file_id ? `file_id ${escapeHtml(doc.file_id)} • ` : ''}${escapeHtml(doc.dataset_stage || doc.stage || '')}${doc.dataset_split ? ` • ${escapeHtml(doc.dataset_split)}` : ''}${doc.dataset_section ? ` • ${escapeHtml(doc.dataset_section)}` : ''}</div>
              </div>
              <div class="row gap wrap">
                ${doc.is_current_approved ? '<span class="pill selected">актуальная утвержденная</span>' : ''}
                <span class="pill">${escapeHtml(doc.source_type || 'source')}</span>
                ${doc.source_type === 'rag' && doc.source_document_id ? `<button class="btn ghost small-btn js-sync-source-fragments" data-document-version-id="${escapeHtml(String(doc.id))}">Синхронизировать источники</button>` : ''}
                ${doc.source_type === 'ifc' && doc.source_document_id ? `<button class="btn ghost small-btn js-sync-ifc-observations" data-document-version-id="${escapeHtml(String(doc.id))}">Синхронизировать IFC</button>` : ''}
              </div>
            </div>
          `).join('')}
        </div>
      </article>
    `).join('');
  }

  async function syncSourceFragments(documentVersionId){
    if(!documentVersionId) return;
    setStatus('Синхронизирую источники документа…', 'warn');
    const result = await fetchJSON(`/api/case10/document-versions/${encodeURIComponent(documentVersionId)}/sync-source-fragments`, {method:'POST', headers: authHeaders()});
    await refreshDocumentVersions();
    setStatus(`Источники синхронизированы: +${result.created || 0}, обновлено ${result.updated || 0}`);
  }

  async function syncIfcObservations(documentVersionId){
    if(!documentVersionId) return;
    setStatus('Синхронизирую IFC-наблюдения…', 'warn');
    const result = await fetchJSON(`/api/case10/document-versions/${encodeURIComponent(documentVersionId)}/sync-ifc-observations`, {method:'POST', headers: authHeaders()});
    await Promise.allSettled([refreshObjects(), refreshChanges(), refreshOverview()]);
    setStatus(`IFC синхронизирован: объектов +${result.created_entities || 0}, наблюдений +${result.observations_created || 0}`);
  }

  async function refreshMatrix(){
    if(!currentProjectId || !$('#matrixTable')) return;
    const data = await fetchJSON(`/api/case10/matrix?project_id=${currentProjectId}`, {headers: authHeaders()});
    const root = $('#matrixTable');
    const rows = data.params || [];
    if(!rows.length){
      root.innerHTML = '<div class="muted">Матрица пока пуста.</div>';
      return;
    }
    root.innerHTML = `
      <div class="matrix-meta">
        <span class="pill">${escapeHtml(data.matrix?.version || '')}</span>
        <span class="pill">${escapeHtml(String(data.matrix?.params_count || rows.length))} параметров</span>
        <span class="muted small">${escapeHtml(data.matrix?.note || '')}</span>
      </div>
      <table class="case-table">
        <thead><tr><th>Код</th><th>Параметр</th><th>Источники</th><th>Критичность</th><th>Mapping</th><th>Extractor</th></tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td><strong>${escapeHtml(row.matrix_code || row.code)}</strong><div class="muted small">${escapeHtml(row.scoring_code || row.code)}</div><div class="muted small">${escapeHtml(row.section || '')}</div>${row.parameter_id ? `<div class="muted small">id ${escapeHtml(String(row.parameter_id))}</div>` : ''}</td>
              <td>${escapeHtml(row.parameter_name)}${row.unit ? `<div class="muted small">${escapeHtml(row.unit)}</div>` : ''}</td>
              <td>
                ${row.source_pd ? '<span class="pill">ПД</span>' : ''} ${row.source_rd ? '<span class="pill">РД</span>' : ''} ${row.source_id ? '<span class="pill">ИД</span>' : ''}
                <div class="muted small top-gap">${[row.source_pd_ref, row.source_rd_ref, row.source_id_ref].filter(Boolean).map(escapeHtml).join(' / ')}</div>
              </td>
              <td><span class="pill">${escapeHtml(row.criticality || row.review_priority || 'MEDIUM')}</span></td>
              <td>${escapeHtml(row.mapping_status || '')}${row.matrix_row ? `<div class="muted small">row ${escapeHtml(String(row.matrix_row))}</div>` : ''}${row.aliases?.length ? `<div class="muted small">${row.aliases.map(escapeHtml).join(' / ')}</div>` : ''}</td>
              <td><code>${escapeHtml(row.trigger_logic || row.regex_pattern || 'ожидает настройки')}</code></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  async function refreshCandidates(){
    if(!currentProjectId || !$('#candidateCards')) return;
    const rows = await fetchJSON(`/api/case10/evidence-groups?project_id=${currentProjectId}`, {headers: authHeaders()});
    const root = $('#candidateCards');
    if(!rows.length){
      root.innerHTML = '<section class="card muted">Evidence groups пока нет. Загрузите ПД / РД / ИД и запустите проверку.</section>';
      return;
    }
    root.innerHTML = rows.map(renderEvidenceGroupCard).join('');
  }

  function renderEvidenceGroupCard(group){
    const param = group.parameter || {};
    const paramCodeLabel = param.matrix_code && param.scoring_code ? `${param.matrix_code} / ${param.scoring_code}` : (param.matrix_code || param.code || 'PARAM');
    const fragments = group.fragments || [];
    const isLocked = group.process_status === 'FINALIZED';
    return `
      <article class="card evidence-group-card">
        <div class="evidence-group-head">
          <div>
            <div class="row gap wrap">
              <strong>${escapeHtml(paramCodeLabel)}</strong>
              <span class="risk-pill ${findingClass(group.finding_status)}">${escapeHtml(findingStatusLabel(group.finding_status))}</span>
              <span class="pill">${escapeHtml(group.review_priority || 'MEDIUM')}</span>
            </div>
            <h2>${escapeHtml(param.name || 'Параметр')}</h2>
            <div class="muted small">${escapeHtml(group.entity_name || group.object_id || 'Объект')} • ${escapeHtml(group.comparison_scenario || '')}</div>
          </div>
          <div class="candidate-actions">
            <button class="btn ghost small-btn js-evidence-decision" data-evidence-group-id="${escapeHtml(String(group.id))}" data-decision="Confirm" ${isLocked ? 'disabled' : ''}>Confirm</button>
            <button class="btn ghost small-btn js-evidence-decision" data-evidence-group-id="${escapeHtml(String(group.id))}" data-decision="Reject" ${isLocked ? 'disabled' : ''}>Reject</button>
            <button class="btn ghost small-btn js-evidence-decision" data-evidence-group-id="${escapeHtml(String(group.id))}" data-decision="Clarification Required" ${isLocked ? 'disabled' : ''}>Clarify</button>
          </div>
        </div>
        <div class="evidence-values">
          <div><span>Expected</span><strong>${escapeHtml(group.expected || '—')}</strong></div>
          <div><span>Actual</span><strong>${escapeHtml(group.actual || '—')}</strong></div>
          <div><span>Delta</span><strong>${escapeHtml(formatDelta(group.delta))}</strong></div>
        </div>
        <div class="evidence-fragments">
          ${fragments.map(renderEvidenceFragment).join('') || '<div class="muted">Фрагменты evidence пока не найдены.</div>'}
        </div>
      </article>
    `;
  }

  function renderEvidenceFragment(fragment){
    const bbox = formatBBox(fragment.bbox_normalized || fragment.bbox);
    const bboxPdf = formatBBox(fragment.bbox_pdf);
    const pageSize = fragment.page_width && fragment.page_height ? `${Number(fragment.page_width).toFixed(0)}×${Number(fragment.page_height).toFixed(0)}` : '';
    return `
      <div class="evidence-fragment">
        <div class="row gap wrap">
          <strong>${escapeHtml(fragmentRoleLabel(fragment.role))}</strong>
          <span class="pill">${escapeHtml(stageLabel(fragment.stage))}</span>
          <span class="pill">page ${escapeHtml(String(fragment.page || '?'))}</span>
        </div>
        <div class="muted small top-gap">${escapeHtml(fragment.file || '')} • rev ${escapeHtml(fragment.revision || '?')} • ${escapeHtml(fragment.approval_status || 'UNKNOWN')}</div>
        <div class="muted small top-gap">${fragment.file_id ? `file_id ${escapeHtml(fragment.file_id)} • ` : ''}${escapeHtml(fragment.dataset_stage || fragment.stage || '')}${fragment.dataset_split ? ` • ${escapeHtml(fragment.dataset_split)}` : ''}${pageSize ? ` • page ${escapeHtml(pageSize)}` : ''}</div>
        <div class="top-gap"><strong>${escapeHtml(fragment.extracted_value || '')}</strong></div>
        <div class="muted small top-gap">bbox normalized [${escapeHtml(bbox)}]</div>
        ${bboxPdf !== '—' ? `<div class="muted small top-gap">bbox pdf [${escapeHtml(bboxPdf)}]</div>` : ''}
        ${fragment.context ? `<div class="fragment-context">${escapeHtml(clipText(fragment.context, 220))}</div>` : ''}
        ${fragment.file_id && fragment.page ? `<button class="btn ghost small-btn js-evidence-page" data-fragment-id="${escapeHtml(String(fragment.id))}">Открыть страницу с выделением</button>` : ''}
      </div>
    `;
  }

  function formatBBox(value){
    if(!Array.isArray(value)) return '—';
    return value.map(v => {
      const num = Number(v);
      return Number.isFinite(num) ? num.toFixed(Math.abs(num) <= 1 ? 3 : 1) : String(v);
    }).join(', ');
  }

  async function openEvidencePage(button){
    button.disabled = true;
    try {
      const response = await fetch(`/api/case10/evidence-fragments/${encodeURIComponent(button.dataset.fragmentId)}/page.png`, {headers: authHeaders()});
      if(!response.ok){
        const error = await response.json();
        throw new Error(error.detail || 'Страница недоступна');
      }
      const src = URL.createObjectURL(await response.blob());
      const img = $('#lightboxImage');
      img.addEventListener('load', () => URL.revokeObjectURL(src), {once:true});
      openImageLightbox({src, caption: button.closest('.evidence-fragment').querySelector('.muted')?.textContent || ''});
    } finally {
      button.disabled = false;
    }
  }

  function formatDelta(delta){
    if(!delta) return '—';
    if(delta.reason) return delta.reason;
    if(delta.equal) return 'совпадает';
    if(delta.delta !== undefined && delta.delta !== null) return String(delta.delta);
    if(delta.expected !== undefined || delta.actual !== undefined) return `${delta.expected ?? '—'} → ${delta.actual ?? '—'}`;
    return JSON.stringify(delta);
  }

  async function refreshObjects(options){
    if(!currentProjectId || !$('#entityRegistry')) return;
    const rows = await fetchJSON(`/api/entities?project_id=${currentProjectId}`, {headers: authHeaders()});
    const root = $('#entityRegistry');
    if(!rows.length){
      selectedEntityId = null;
      root.innerHTML = '<div class="muted">Объектов пока нет. Создайте synthetic dataset или дождитесь extractor pipeline.</div>';
      const portrait = $('#portraitPanel');
      if(portrait) portrait.innerHTML = 'Выберите объект в реестре.';
      return;
    }
    if(!selectedEntityId || !rows.some(row => row.id === selectedEntityId)) selectedEntityId = rows[0].id;
    root.innerHTML = `
      <table class="case-table">
        <thead><tr><th>Object</th><th>Type</th><th>Aliases</th><th>Stages</th><th>Changes</th><th>Risk</th></tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr class="entity-row ${row.id === selectedEntityId ? 'selected' : ''}" data-entity-id="${escapeHtml(row.id)}">
              <td><button class="link-button js-select-entity" data-entity-id="${escapeHtml(row.id)}">${escapeHtml(row.canonical_name)}</button><div class="muted small">${escapeHtml(row.id)}</div></td>
              <td>${escapeHtml(row.entity_type)}</td>
              <td>${(row.aliases || []).slice(0, 4).map(alias => `<span class="pill">${escapeHtml(alias)}</span>`).join(' ')}</td>
              <td>${(row.stages || []).map(stage => `<span class="pill">${escapeHtml(stageLabel(stage))}</span>`).join(' ')}</td>
              <td>${escapeHtml(String(row.changes_count || 0))}</td>
              <td><span class="risk-pill ${riskClass(row.risk?.severity)}">${escapeHtml(row.risk?.severity || 'NONE')} ${Math.round(row.risk?.score || 0)}</span></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
    if(!(options && options.skipPortrait)) await refreshPortrait(selectedEntityId);
  }

  async function refreshPortrait(entityId){
    if(!entityId || !$('#portraitPanel')) return;
    selectedEntityId = entityId;
    const data = await fetchJSON(`/api/entities/${encodeURIComponent(entityId)}/portrait`, {headers: authHeaders()});
    renderPortrait(data);
  }

  function renderPortrait(data){
    const root = $('#portraitPanel');
    if(!root) return;
    const identity = data.identity || {};
    const aliases = data.aliases || [];
    const attrs = data.attributes || {};
    const locations = data.locations || [];
    const relations = data.relations || [];
    const timeline = data.timeline || [];
    const changes = data.changes || [];
    root.classList.remove('muted');
    root.innerHTML = `
      <div class="portrait-head">
        <div>
          <h2>${escapeHtml(identity.canonical_name || 'Object')}</h2>
          <div class="muted small">${escapeHtml(identity.id || '')} • ${escapeHtml(identity.entity_type || '')}</div>
        </div>
        <span class="risk-pill ${riskClass(data.risk?.severity)}">${escapeHtml(data.risk?.severity || 'NONE')} ${Math.round(data.risk?.score || 0)}</span>
      </div>
      <div class="portrait-section">
        <div class="section-title">Aliases</div>
        <div class="tag-list">${aliases.map(alias => `<span class="pill">${escapeHtml(alias.alias)}</span>`).join('') || '<span class="muted">Нет alias</span>'}</div>
      </div>
      <div class="portrait-section">
        <div class="section-title">Location</div>
        ${locations.map(loc => `<div class="kv-line">${Object.entries(loc).map(([k,v]) => `<span>${escapeHtml(k)}: <strong>${escapeHtml(v)}</strong></span>`).join('')}</div>`).join('') || '<div class="muted">Не указано</div>'}
      </div>
      <div class="portrait-section">
        <div class="section-title">Properties</div>
        <div class="attribute-grid">
          ${Object.entries(attrs).map(([name, values]) => `
            <div class="attribute-row">
              <strong>${escapeHtml(name)}</strong>
              <div>${values.map(v => `<span class="pill">${escapeHtml(stageLabel(v.stage))}: ${escapeHtml(formatValueWithUnit(v.raw_value || v.value || '', v.unit))}</span>`).join(' ')}</div>
            </div>
          `).join('') || '<div class="muted">Нет свойств</div>'}
        </div>
      </div>
      <div class="portrait-section">
        <div class="section-title">Timeline</div>
        <div class="timeline">
          ${timeline.map(point => `
            <div class="timeline-point">
              <strong>${escapeHtml(stageLabel(point.stage))}</strong>
              <div>${Object.entries(point.attributes || {}).map(([name, item]) => `<span>${escapeHtml(name)}: <b>${escapeHtml(item.raw_value || item.value || '')}</b></span>`).join('')}</div>
            </div>
          `).join('') || '<div class="muted">Нет timeline</div>'}
        </div>
      </div>
      <div class="portrait-section">
        <div class="section-title">Relations</div>
        ${relations.map(rel => `<div class="kv-line"><span>${escapeHtml(rel.relation_type)}</span><strong>${escapeHtml(rel.entity_name || rel.entity_id)}</strong></div>`).join('') || '<div class="muted">Связи пока не найдены</div>'}
      </div>
      <div class="portrait-section">
        <div class="section-title">Changes</div>
        ${changes.slice(0, 6).map(ch => `<div class="change-chip"><span>${escapeHtml(ch.change_type)}</span><strong>${escapeHtml(ch.attribute)}</strong></div>`).join('') || '<div class="muted">Изменений пока нет</div>'}
      </div>
      <div class="portrait-section">
        <div class="section-title">Sources</div>
        ${(data.evidence || []).slice(0, 4).map(src => `<div class="source-mini">${escapeHtml(src.filename || 'source')} • ${escapeHtml(stageLabel(src.stage))} • стр. ${escapeHtml(String(src.page || '?'))}<div class="muted small">${escapeHtml(clipText(src.text || '', 140))}</div></div>`).join('') || '<div class="muted">Источники пока не привязаны</div>'}
      </div>
    `;
  }

  async function refreshChanges(){
    if(!currentProjectId || !$('#changesTable')) return;
    const rows = await fetchJSON(`/api/case10/changes?project_id=${currentProjectId}`, {headers: authHeaders()});
    const root = $('#changesTable');
    if(!rows.length){
      root.innerHTML = '<div class="muted">Изменений пока нет. Change Engine заполнится после observations.</div>';
      return;
    }
    root.innerHTML = `
      <table class="case-table">
        <thead><tr><th>Entity</th><th>Parameter</th><th>П</th><th>Р</th><th>И</th><th>Risk</th><th>Confidence</th><th>Status</th></tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td><button class="link-button js-select-entity" data-entity-id="${escapeHtml(row.entity_id)}">${escapeHtml(row.entity_name)}</button><div class="muted small">${escapeHtml(row.change_type)}</div></td>
              <td>${escapeHtml(row.parameter)}</td>
              <td>${renderStageValue(row.stage_values?.project)}</td>
              <td>${renderStageValue(row.stage_values?.working)}</td>
              <td>${renderStageValue(row.stage_values?.as_built)}</td>
              <td><span class="risk-pill ${riskClass(row.risk?.severity)}">${escapeHtml(row.risk?.severity || 'NONE')} ${Math.round(row.risk?.score || 0)}</span></td>
              <td>${Math.round((row.confidence || 0) * 100)}%</td>
              <td>${escapeHtml(row.status || 'New')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
    const first = rows[0];
    const shell = $('#evidenceShell');
    if(shell && first){
      shell.innerHTML = `
        <div><strong>source A</strong><span>${escapeHtml(first.delta?.from?.stage ? stageLabel(first.delta.from.stage) : 'A')}</span><p>${escapeHtml(first.delta?.from?.raw_value || 'Будущий лист/fragment')}</p></div>
        <div><strong>source B</strong><span>${escapeHtml(first.delta?.to?.stage ? stageLabel(first.delta.to.stage) : 'B')}</span><p>${escapeHtml(first.delta?.to?.raw_value || 'Будущий лист/fragment')}</p></div>
      `;
    }
  }

  function renderStageValue(item){
    if(!item) return '<span class="muted">—</span>';
    return `<strong>${escapeHtml(formatValueWithUnit(item.raw_value || item.value || '', item.unit))}</strong>`;
  }

  async function refreshProtocol(){
    if(!currentProjectId || !$('#protocolBoard')) return;
    const protocol = await fetchJSON(`/api/case10/protocols/current?project_id=${currentProjectId}`, {headers: authHeaders()});
    const root = $('#protocolBoard');
    if(!protocol){
      root.innerHTML = '<section class="card muted">Протокол еще не создан. Запустите проверку.</section>';
      return;
    }
    const payload = protocol.payload || {};
    const sections = payload.sections || {};
    const findings = payload.findings || [];
    root.innerHTML = `
      <section class="card protocol-summary">
        <div class="row gap wrap">
          <div>
            <strong>Protocol v${escapeHtml(String(protocol.version))}</strong>
            <div class="muted small">${escapeHtml(protocol.process_id)} • ${escapeHtml(protocol.status)}</div>
          </div>
          <div class="row gap wrap">
            <button class="btn ghost small-btn js-download-protocol-pdf" data-protocol-id="${escapeHtml(String(protocol.id))}">PDF</button>
            <button class="btn primary small-btn js-finalize-protocol" data-protocol-id="${escapeHtml(String(protocol.id))}" ${protocol.status === 'FINALIZED' ? 'disabled' : ''}>FINALIZE</button>
          </div>
        </div>
        <div class="inspection-grid top-gap">
          <div><span>Matrix</span><strong>${escapeHtml(protocol.matrix_version || '')}</strong></div>
          <div><span>Dataset</span><strong>${escapeHtml(protocol.dataset_version || '')}</strong></div>
          <div><span>Model</span><strong>${escapeHtml(protocol.model_version || '')}</strong></div>
          <div><span>Manifest</span><strong>${escapeHtml(String(protocol.input_manifest_hash || '').slice(0, 12) || '—')}</strong></div>
        </div>
      </section>
      <section class="protocol-board-inner">
        ${Object.entries(sections).map(([key, value]) => `
          <article class="protocol-column">
            <div class="protocol-head">${escapeHtml(key.replaceAll('_', ' '))}</div>
            <div class="protocol-explanation">${escapeHtml(typeof value === 'object' ? JSON.stringify(value) : String(value))}</div>
          </article>
        `).join('')}
      </section>
      <section class="card">
        <div class="card-title">Findings</div>
        <div class="protocol-findings">
          ${findings.slice(0, 40).map((item) => {
            const param = item.parameter || {};
            return `
              <div class="protocol-card">
                <div class="row gap wrap">
                  <strong>${escapeHtml(param.code || '')}</strong>
                  <span class="risk-pill ${findingClass(item.finding_status)}">${escapeHtml(findingStatusLabel(item.finding_status))}</span>
                </div>
                <div class="muted small top-gap">${escapeHtml(param.name || '')} • ${escapeHtml(item.entity_name || item.object_id || '')}</div>
                <div class="protocol-explanation">${escapeHtml(item.expected || '—')} → ${escapeHtml(item.actual || '—')}</div>
              </div>
            `;
          }).join('') || '<div class="muted">Findings пока нет.</div>'}
        </div>
      </section>
    `;
  }

  async function createIssueDecision(issueId, decision){
    if(!issueId) return;
    await fetchJSON(`/api/issues/${encodeURIComponent(issueId)}/decisions`, {method:'POST', headers: authHeaders(), body: JSON.stringify({decision})});
    await Promise.allSettled([refreshProtocol(), refreshChanges(), refreshObjects({skipPortrait:true}), refreshOverview()]);
    setStatus(`Решение сохранено: ${decision}`);
  }

  async function createEvidenceDecision(evidenceGroupId, decision){
    if(!evidenceGroupId) return;
    let reasonCode = null;
    let comment = null;
    if(decision === 'Reject'){
      reasonCode = window.prompt('Reason code для отклонения', 'OTHER') || 'OTHER';
      comment = window.prompt('Комментарий инспектора', '') || '';
    } else if(decision === 'Clarification Required'){
      comment = window.prompt('Что нужно уточнить?', '') || '';
    }
    await fetchJSON(`/api/case10/evidence-groups/${encodeURIComponent(evidenceGroupId)}/decisions`, {
      method:'POST',
      headers: authHeaders(),
      body: JSON.stringify({decision, reason_code: reasonCode, comment}),
    });
    await Promise.allSettled([refreshCandidates(), refreshProtocol(), refreshOverview()]);
    setStatus(`Решение инспектора сохранено: ${decision}`);
  }

  async function finalizeProtocol(protocolId){
    if(!protocolId) return;
    const ok = window.confirm('Финализировать протокол? После этого дозагрузка и решения в текущей проверке будут заблокированы.');
    if(!ok) return;
    await fetchJSON(`/api/case10/protocols/${encodeURIComponent(protocolId)}/finalize`, {method:'POST', headers: authHeaders()});
    await Promise.allSettled([refreshProtocol(), refreshCandidates(), refreshOverview()]);
    setStatus('Протокол финализирован');
  }

  function addMessage(role, text, sources, selectedChunkIds, assets, selectedAssetIds, pages){
    chatState.push({
      role,
      text,
      sources: Array.isArray(sources) ? sources : [],
      selected_chunk_ids: Array.isArray(selectedChunkIds) ? selectedChunkIds : [],
      assets: Array.isArray(assets) ? assets : [],
      selected_asset_ids: Array.isArray(selectedAssetIds) ? selectedAssetIds : [],
      pages: Array.isArray(pages) ? pages : [],
      viewer_index: 0,
      at: new Date().toISOString(),
    });
    saveChatState();
    renderChat();
  }

  function renderPageAction(pageId, filename, pageNumber){
    if(!pageId) return '';
    return `<button class="btn ghost small-btn js-download-page" data-page-id="${escapeHtml(String(pageId))}" data-filename="${escapeHtml(filename || 'sheet')}" data-page-number="${escapeHtml(String(pageNumber || ''))}">Скачать лист</button>`;
  }

  function renderSourceCard(source){
    const title = `${source.filename}${source.page_number ? ` • лист ${source.page_number}` : ''}`;
    return `
      <article class="evidence-card text-card">
        <div class="evidence-card-head simple-head">
          <div>
            <div class="evidence-card-title">${escapeHtml(title)}</div>
            <div class="evidence-card-sub">Текстовый фрагмент, на котором основан ответ</div>
          </div>
          ${renderPageAction(source.page_id, source.filename, source.page_number)}
        </div>
        <div class="evidence-card-body">${escapeHtml(clipText(source.text, 640)).replace(/\n/g,'<br>')}</div>
      </article>
    `;
  }

  function renderAssetCard(asset){
    const imageHtml = asset.has_image ? `<div class="asset-preview"><img class="protected-region-image clickable-image" data-region-id="${escapeHtml(String(asset.region_id))}" alt="${escapeHtml(regionLabel(asset.region_type))}" title="Нажмите, чтобы увеличить" loading="lazy" /></div>` : '';
    const bodyText = clipText(asset.summary || asset.ocr_text || '', 320) || 'Откройте лист — на нём есть нужный фрагмент.';
    return `
      <article class="evidence-card asset-card">
        ${imageHtml}
        <div class="evidence-card-head simple-head compact">
          <div>
            <div class="evidence-card-title">${escapeHtml(regionLabel(asset.region_type))}</div>
            <div class="evidence-card-sub">${escapeHtml(asset.filename)}${asset.page_number ? ` • лист ${asset.page_number}` : ''}</div>
          </div>
          ${renderPageAction(asset.page_id, asset.filename, asset.page_number)}
        </div>
        <div class="evidence-card-body">${escapeHtml(bodyText).replace(/\n/g,'<br>')}</div>
      </article>
    `;
  }

  function deriveViewerPages(message){
    const explicit = Array.isArray(message.pages) ? message.pages.filter(p => Number(p?.page_id) > 0) : [];
    if(explicit.length) return explicit;

    const selectedChunkIds = new Set((message.selected_chunk_ids || []).map(Number));
    const selectedAssetIds = new Set((message.selected_asset_ids || []).map(Number));
    const out = [];
    const seen = new Set();
    const add = (item) => {
      const pageId = Number(item?.page_id || 0);
      if(!pageId || seen.has(pageId)) return;
      seen.add(pageId);
      out.push({
        page_id: pageId,
        document_id: Number(item?.document_id || 0),
        filename: item?.filename || 'Документ',
        page_number: Number(item?.page_number || 0),
      });
    };

    (message.sources || []).filter(s => s.selected || (s.chunk_id != null && selectedChunkIds.has(Number(s.chunk_id)))).forEach(add);
    (message.assets || []).filter(a => a.selected || selectedAssetIds.has(Number(a.region_id))).forEach(add);
    if(!out.length){
      (message.sources || []).slice(0, 3).forEach(add);
      (message.assets || []).slice(0, 3).forEach(add);
    }
    return out;
  }

  function renderPdfViewer(message, messageIndex){
    const pages = deriveViewerPages(message);
    if(!pages.length) return '';
    const currentIndex = Math.max(0, Math.min(Number(message.viewer_index || 0), pages.length - 1));
    const current = pages[currentIndex];
    const prevDisabled = currentIndex <= 0 ? 'disabled' : '';
    const nextDisabled = currentIndex >= pages.length - 1 ? 'disabled' : '';
    const fileTitle = `${current.filename}${current.page_number ? ` • лист ${current.page_number}` : ''}`;
    const pageIds = pages.map(p => Number(p.page_id)).filter(Boolean).join(',');

    return `
      <section class="pdf-viewer" data-chat-index="${messageIndex}">
        <div class="pdf-viewer-head">
          <div>
            <div class="pdf-viewer-title">Листы из документации</div>
            <div class="pdf-viewer-sub">${escapeHtml(fileTitle)}</div>
          </div>
          <button class="btn ghost small-btn js-download-pages-pdf" data-page-ids="${escapeHtml(pageIds)}" data-filename="${escapeHtml(current.filename || 'selected_pages')}">Скачать PDF</button>
        </div>
        <div class="pdf-viewer-stage">
          <img class="protected-page-image clickable-image pdf-viewer-image" data-page-id="${escapeHtml(String(current.page_id))}" alt="${escapeHtml(fileTitle)}" title="Нажмите, чтобы увеличить" loading="lazy" />
        </div>
        <div class="pdf-viewer-controls">
          <button class="btn ghost small-btn js-viewer-nav" data-chat-index="${messageIndex}" data-direction="prev" ${prevDisabled}>← Предыдущий</button>
          <div class="pdf-viewer-counter">${currentIndex + 1} / ${pages.length}</div>
          <button class="btn ghost small-btn js-viewer-nav" data-chat-index="${messageIndex}" data-direction="next" ${nextDisabled}>Следующий →</button>
        </div>
      </section>
    `;
  }

  function renderChat(){
    const root = $('#chatMessages'); root.innerHTML='';
    chatState.forEach((m, idx) => {
      const div=document.createElement('div'); div.className=`msg ${m.role}`;
      const bodyHtml = m.role === 'assistant' ? renderRichText(m.text) : `<p>${escapeHtml(m.text).replace(/\n/g,'<br>')}</p>`;
      const evidenceHtml = m.role === 'assistant' ? renderPdfViewer(m, idx) : '';
      div.innerHTML = `
        <div class="meta"><span>${m.role === 'user' ? (me?.login || 'Вы') : 'Ассистент'}</span><span>${fmtDate(m.at)}</span></div>
        <div class="body rich-text">${bodyHtml}</div>
        ${evidenceHtml}
      `;
      root.appendChild(div);
    });
    hydrateProtectedImages(root).catch(() => {});
    hydrateProtectedPageImages(root).catch(() => {});
    root.scrollTop = root.scrollHeight;
  }

  function finishChatProcessing(){
    clearChatJobPoll();
    setChatProcessing(false);
    sendInFlight = false;
    setQuestionControlsDisabled(false);
    $('#questionInput')?.focus();
    if(!$('#docsView')?.classList.contains('hidden')) startDocsPolling();
  }

  async function pollChatJob(jobId, pending, attempt=0){
    clearChatJobPoll();
    const maxAttempts = Number(pending?.maxAttempts || 240); // ~20 min at 5 sec
    try{
      const data = await fetchJSON(`/api/chat/jobs/${encodeURIComponent(jobId)}`, {headers: authHeaders()});
      const status = String(data.status || '').toLowerCase();
      const stage = data.stage || 'Модель анализирует документы…';
      setChatProcessing(true, stage);
      setStatus(stage, status === 'error' ? 'error' : 'warn');

      if(status === 'done'){
        const result = data.result || {};
        addMessage('assistant', result.answer || 'Ответ получен, но тело ответа пустое.', result.sources || [], result.selected_chunk_ids || [], result.assets || [], result.selected_asset_ids || [], result.pages || []);
        setPendingChat(null);
        setStatus('Ответ готов');
        finishChatProcessing();
        return;
      }
      if(status === 'error'){
        addMessage('assistant', `### Ошибка\n${data.error || 'Backend не смог выполнить LLM-задачу.'}`, [], [], [], [], []);
        setPendingChat(null);
        setStatus('Ошибка ответа LLM', 'error');
        finishChatProcessing();
        return;
      }
      if(attempt >= maxAttempts){
        addMessage('assistant', '### Ошибка\nИстекло время ожидания LLM-задачи. Проверь историю проекта: если ответ всё же был сохранён, его можно загрузить кнопкой «История проекта».', [], [], [], [], []);
        setPendingChat(null);
        setStatus('Истекло время ожидания', 'error');
        finishChatProcessing();
        return;
      }
      chatJobPollTimer = setTimeout(() => pollChatJob(jobId, pending, attempt + 1), 5000);
    }catch(e){
      if(attempt >= maxAttempts){
        addMessage('assistant', `### Ошибка\nНе удалось получить статус LLM-задачи. ${e.message || e}`, [], [], [], [], []);
        setPendingChat(null);
        setStatus('Ошибка связи с backend', 'error');
        finishChatProcessing();
        return;
      }
      setChatProcessing(true, 'Связь с backend временно оборвалась. Продолжаю ждать результат задачи…');
      setStatus('Ожидаем восстановление связи с backend…', 'warn');
      chatJobPollTimer = setTimeout(() => pollChatJob(jobId, pending, attempt + 1), 5000);
    }
  }

  async function resumePendingChatIfAny(){
    const pending = getPendingChat();
    if(!pending?.job_id) return;
    if(pending.project_id && Number(pending.project_id) !== Number(currentProjectId)){
      currentProjectId = Number(pending.project_id);
      const sel = $('#projectSelect');
      if(sel) sel.value = String(currentProjectId);
      restoreChatState();
      renderChat();
    }
    sendInFlight = true;
    clearError();
    stopDocsPolling();
    setQuestionControlsDisabled(true);
    if(pending.question && !hasUserMessage(pending.question)){
      addMessage('user', pending.question, [], [], [], [], []);
    }
    setChatProcessing(true, 'Восстанавливаю ожидание ответа по уже запущенному запросу…');
    pollChatJob(pending.job_id, pending).catch(() => {});
  }

  async function sendQuestion(){
    if(sendInFlight) return;
    if(!currentProjectId) throw new Error('Сначала создайте или выберите проект');
    const input = $('#questionInput');
    const question = input.value.trim();
    if(!question) return;

    sendInFlight = true;
    clearError();
    clearChatJobPoll();
    stopDocsPolling();
    addMessage('user', question, [], [], [], [], []);
    input.value='';
    setQuestionControlsDisabled(true);
    setChatProcessing(true, 'Запускаем анализ документов. Backend выполнит долгий запрос в фоне, а интерфейс будет только опрашивать статус.');
    setStatus('Запускаем LLM-задачу…', 'warn');

    try{
      const started = await fetchJSON('/api/chat/jobs', {
        method:'POST',
        headers: authHeaders(),
        body: JSON.stringify({project_id: Number(currentProjectId), question, user_label: me?.login || null, top_k: Number($('#topK')?.value || 5)})
      });
      const pending = {
        job_id: started.job_id,
        question,
        project_id: Number(currentProjectId),
        started_at: new Date().toISOString(),
        maxAttempts: 240,
      };
      setPendingChat(pending);
      setChatProcessing(true, started.stage || 'Задача запущена. Ждём ответ модели…');
      await pollChatJob(started.job_id, pending);
    }catch(e){
      addMessage('assistant', `### Ошибка\nНе удалось запустить LLM-задачу. ${e.message || e}`, [], [], [], [], []);
      setPendingChat(null);
      setStatus('Ошибка запуска LLM-задачи', 'error');
      finishChatProcessing();
    }
  }

  async function deleteDocument(doc){
    if(!currentProjectId) throw new Error('Сначала выберите проект');
    const sourceType = doc.source_type || (String(doc.filename || '').toLowerCase().endsWith('.ifc') ? 'ifc' : 'rag');
    const ok = window.confirm(`Удалить документ "${doc.filename}"?\n\nБудет удалён сохранённый файл и его метаданные.`);
    if(!ok) return;
    await fetchJSON(`/api/documents/${doc.id}?project_id=${currentProjectId}&source_type=${encodeURIComponent(sourceType)}`, {method:'DELETE', headers: authHeaders()});
    setStatus(`Удалён ${doc.filename}`);
    await refreshDocs();
  }

  async function reindexDocument(doc){
    if(!currentProjectId) throw new Error('Сначала выберите проект');
    const sourceType = doc.source_type || 'rag';
    const status = String(doc.processing_status || doc.status || '').trim();
    const isFailed = status === 'failed';
    const isUploadJob = sourceType === 'upload_job';
    if(sourceType !== 'rag' && !isUploadJob) throw new Error('Пересборка доступна только для RAG-документов');
    const actionText = isUploadJob ? (isFailed ? 'Повторить обработку' : 'Запустить обработку') : (isFailed ? 'Повторить обработку' : 'Пересобрать индекс');
    const ok = window.confirm(`${actionText} для "${doc.filename}"?\n\nИсходный файл останется на сервере, старые поисковые фрагменты будут заменены.`);
    if(!ok) return;
    setStatus(`${isUploadJob ? actionText : (isFailed ? 'Повторяю обработку' : 'Пересобираю индекс')}: ${doc.filename}`, 'warn');
    if(isUploadJob){
      await fetchJSON(`/api/upload-jobs/${doc.id}/process?project_id=${currentProjectId}`, {method:'POST', headers: authHeaders()});
      setStatus(`Обработка поставлена в очередь: ${doc.filename}`, 'warn');
      startDocsPolling();
    } else {
      const result = await fetchJSON(`/api/documents/${doc.id}/reindex?project_id=${currentProjectId}&source_type=${encodeURIComponent(sourceType)}`, {method:'POST', headers: authHeaders()});
      setStatus(`${isFailed ? 'Обработка повторена' : 'Индекс пересобран'}: ${result.pages_created || 0} стр., ${result.chunks_created || 0} фрагм.`);
    }
    await refreshDocs();
  }

  async function refreshDocs(options){
    if(!currentProjectId) return;
    const rows = await fetchJSON(`/api/documents?project_id=${currentProjectId}`, {headers: authHeaders()});
    refreshDocumentVersions().catch(() => {});
    const root = $('#docsList'); root.innerHTML='';
    let hasActiveUploads = false;
    rows.forEach(r => {
      const div=document.createElement('div');
      const sourceType = r.source_type || (String(r.filename || '').toLowerCase().endsWith('.ifc') ? 'ifc' : 'rag');
      const isUploadJob = sourceType === 'upload_job';
      const processingStatus = String(r.processing_status || r.status || '').trim();
      const explicitStatus = processingStatus || String(r.status || '').trim();
      const isReady = typeof r.is_ready === 'boolean' ? r.is_ready : (explicitStatus ? explicitStatus === 'ready' : !isUploadJob);
      const isFailed = explicitStatus === 'failed';
      const isActive = !isReady && !isFailed && (!isUploadJob || explicitStatus === 'processing');
      if(isActive) hasActiveUploads = true;
      div.className = `item ${isUploadJob ? 'upload-item' : ''} ${isActive ? 'is-processing' : ''} ${isFailed ? 'is-failed' : ''}`;
      const typeLabel = isUploadJob ? 'Загрузка документа' : (sourceType === 'ifc' ? 'IFC-модель' : 'Документ');
      const canDownloadOriginal = ['rag','simple','upload_job'].includes(sourceType) && !isUploadJob && isReady;
      const canReindex = ((sourceType === 'rag' && !isUploadJob) || isUploadJob) && !isActive;
      const canDelete = true;
      const reindexLabel = isUploadJob ? (isFailed ? 'Повторить обработку' : 'Запустить обработку') : (isFailed ? 'Повторить обработку' : 'Пересобрать индекс');
      const stageText = r.stage ? escapeHtml(r.stage) : '';
      const detailValue = r.error_message || r.detail || '';
      const detailText = detailValue ? `<div class="muted small top-gap">${escapeHtml(detailValue)}</div>` : '';
      const rawProgress = r.processing_progress ?? r.progress;
      const progressValue = Math.max(0, Math.min(100, Number(rawProgress ?? (isReady ? 100 : 0))));
      const statusBadge = explicitStatus ? `<span class="upload-badge status-${escapeHtml(explicitStatus)}">${escapeHtml(uploadStatusLabel(explicitStatus))}</span>` : '';
      const shouldShowProgress = isUploadJob || !!explicitStatus;
      const progressHtml = shouldShowProgress ? `<div class="upload-progress-wrap top-gap"><div class="upload-progress-meta"><span>${statusBadge}${stageText ? ` • ${stageText}` : ''}</span><strong>${progressValue}%</strong></div><div class="upload-progress"><div class="upload-progress-bar" style="width:${progressValue}%"></div></div></div>` : '';
      const unavailableHtml = (!canReindex && !canDownloadOriginal && !canDelete) ? '<span class="muted small">Документ пока недоступен</span>' : '';
      div.innerHTML = `<div class="row gap wrap"><div><strong>${escapeHtml(r.filename)}</strong><div class="muted small top-gap">${typeLabel} • ${fmtDate(r.uploaded_at)}</div>${progressHtml}${detailText}</div><div class="row gap wrap">${canReindex ? `<button class="btn ghost doc-reindex-btn">${reindexLabel}</button>` : ''}${canDownloadOriginal ? '<button class=\"btn ghost doc-download-btn\">Скачать оригинал</button>' : ''}${canDelete ? '<button class="btn danger doc-delete-btn">Удалить</button>' : ''}${unavailableHtml}</div></div>`;
      div.querySelector('.doc-delete-btn')?.addEventListener('click', () => deleteDocument(r).catch(showError));
      div.querySelector('.doc-reindex-btn')?.addEventListener('click', () => reindexDocument(r).catch(showError));
      if(canDownloadOriginal){
        div.querySelector('.doc-download-btn')?.addEventListener('click', () => downloadProtectedFile(buildDocumentDownloadUrl(r.id, currentProjectId, sourceType), r.filename || 'document').catch(showError));
      }
      root.appendChild(div);
    });
    if(hasActiveUploads){
      if(!(options && options.silent)) setStatus('Файл сохраняется. После появления статуса «Готов» его можно использовать в чате.', 'warn');
      startDocsPolling();
    } else if(!$('#docsView')?.classList.contains('hidden')) {
      stopDocsPolling();
    }
  }

  async function uploadFile(){
    if(!currentProjectId) throw new Error('Сначала выберите проект');
    const file = $('#fileInput').files[0];
    if(!file) throw new Error('Выберите файл');
    const form = new FormData();
    form.append('project_id', String(currentProjectId));
    form.append('file_type', $('#fileType').value);
    form.append('file', file);

    const progressWrap = $('#uploadProgressWrap');
    const progressBar = $('#uploadProgressBar');
    const progressLabel = $('#uploadProgressLabel');
    progressWrap?.classList.remove('hidden');
    if(progressBar) progressBar.style.width = '0%';
    if(progressLabel) progressLabel.textContent = 'Подготовка к загрузке… 0%';
    setStatus('Файл загружается на сервер…', 'warn');

    const data = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', api('/api/upload'));
      xhr.responseType = 'json';
      xhr.setRequestHeader('Authorization', `Bearer ${token()}`);
      xhr.upload.onprogress = (event) => {
        if(!event.lengthComputable) return;
        const percent = Math.max(0, Math.min(100, Math.round((event.loaded / event.total) * 100)));
        if(progressBar) progressBar.style.width = `${percent}%`;
        if(progressLabel) progressLabel.textContent = `Загрузка файла на сервер… ${percent}%`;
      };
      xhr.onerror = () => reject(new Error('Ошибка сети при загрузке файла'));
      xhr.onload = () => {
        const response = xhr.response || (() => { try { return JSON.parse(xhr.responseText || '{}'); } catch(e){ return {}; } })();
        if(xhr.status < 200 || xhr.status >= 300){
          reject(new Error(response?.detail || `HTTP ${xhr.status}`));
          return;
        }
        resolve(response);
      };
      xhr.send(form);
    });

    if(progressBar) progressBar.style.width = '100%';
    if(progressLabel) progressLabel.textContent = 'Файл загружен. Дальнейший статус смотри в списке документов…';
    $('#uploadResult').textContent = `Файл принят в очередь обработки: ${data.filename}`;
    setView('docsView');
    await refreshDocs();
    setStatus('Файл принят. Статус обработки обновляется в списке документов.', 'warn');
  }

  async function refreshHistory(){
    if(!currentProjectId) return;
    const rows = await fetchJSON(`/api/history?project_id=${currentProjectId}`, {headers: authHeaders()});
    const root = $('#historyList'); root.innerHTML='';
    rows.forEach(r => {
      const div=document.createElement('div'); div.className='item';
      div.innerHTML = `
        <div><strong>${escapeHtml(r.question)}</strong></div>
        <div class="muted small">${fmtDate(r.created_at)}${r.user_label ? ` • ${escapeHtml(r.user_label)}` : ''}</div>
        <div class="top-gap rich-text">${renderRichText(r.answer)}</div>
      `;
      root.appendChild(div);
    });
  }

  async function loadHistoryIntoChat(){
    if(!currentProjectId) return;
    const rows = await fetchJSON(`/api/history?project_id=${currentProjectId}`, {headers: authHeaders()});
    chatState = [];
    rows.slice().reverse().forEach(r => {
      let parsedRefs = {};
      try{ parsedRefs = JSON.parse(r.references || '{}'); }catch(e){ parsedRefs = {}; }
      const refs = normalizeRefs(parsedRefs);
      chatState.push({role:'user', text:r.question, sources:[], selected_chunk_ids:[], assets:[], selected_asset_ids:[], pages:[], viewer_index:0, at:r.created_at});
      chatState.push({role:'assistant', text:r.answer, sources:refs.sources, selected_chunk_ids:refs.selected_chunk_ids, assets:refs.assets, selected_asset_ids:refs.selected_asset_ids, pages:refs.pages || [], viewer_index:0, at:r.created_at});
    });
    renderChat();
  }

  async function refreshOrgs(){
    const rows = await fetchJSON('/api/admin/orgs', {headers: authHeaders()});
    ['#orgSelect', '#userOrgSelect', '#analyticsOrgSelect'].forEach(selName => {
      const sel=$(selName); if(!sel) return;
      sel.innerHTML='';
      rows.forEach(r => {
        const o=document.createElement('option'); o.value=r.id; o.textContent=r.name; sel.appendChild(o);
      });
    });
    $('#orgList').innerHTML = rows.map(r => `<div class="item"><strong>${escapeHtml(r.name)}</strong></div>`).join('');
  }

  async function createOrg(){
    await fetchJSON('/api/admin/orgs', {method:'POST', headers: authHeaders(), body: JSON.stringify({name: $('#orgNameInput').value.trim()})});
    $('#orgNameInput').value=''; await refreshOrgs();
  }

  async function createUser(){
    const data = await fetchJSON('/api/admin/users', {method:'POST', headers: authHeaders(), body: JSON.stringify({login: $('#userLoginInput').value.trim(), email: $('#userEmailInput').value.trim() || null, organization_id: Number($('#userOrgSelect').value), is_admin: $('#userIsAdminInput').checked})});
    $('#userLoginInput').value=''; $('#userEmailInput').value='';
    await refreshUsers();
    await refreshCredentials();
    downloadTextFile(makeAccountsTxt([data], 'Созданный аккаунт'), `account_${data.login || 'user'}.txt`);
    setStatus(`Аккаунт ${data.login} создан. TXT-файл скачан.`, 'ok');
  }

  async function createBatch(){
    const rows = await fetchJSON('/api/admin/users/batch', {method:'POST', headers: authHeaders(), body: JSON.stringify({organization_id: Number($('#userOrgSelect').value), count: Number($('#batchCountInput').value), login_prefix: $('#batchPrefixInput').value.trim(), is_admin: false})});
    await refreshUsers();
    await refreshCredentials();
    downloadTextFile(makeAccountsTxt(rows, 'Созданные аккаунты'), `accounts_${Date.now()}.txt`);
    setStatus(`Создано аккаунтов: ${rows.length}. TXT-файл скачан.`, 'ok');
  }

  async function refreshUsers(){
    const orgId = $('#userOrgSelect').value;
    const rows = await fetchJSON(`/api/admin/users${orgId ? `?organization_id=${orgId}` : ''}`, {headers: authHeaders()});
    $('#userList').innerHTML = rows.map(r => `<div class="item"><strong>${escapeHtml(r.login)}</strong><div class="muted small">${r.is_admin ? 'Админ' : 'Пользователь'}</div></div>`).join('');
  }

  async function refreshCredentials(){
    if(!me?.is_admin) return;
    const orgId = $('#userOrgSelect')?.value;
    const data = await fetchJSON(`/api/admin/user-credentials${orgId ? `?organization_id=${encodeURIComponent(orgId)}&limit=200` : '?limit=200'}`, {headers: authHeaders()});
    const items = Array.isArray(data.items) ? data.items : [];
    const root = $('#credentialsList');
    if(!root) return;
    if(!items.length){
      root.innerHTML = '<div class="item muted">Данных созданных аккаунтов пока нет. Пароли появятся для аккаунтов, созданных после этого обновления.</div>';
      return;
    }
    root.innerHTML = items.map((r) => `
      <div class="item credential-item">
        <div class="row gap wrap">
          <strong>${escapeHtml(r.login)}</strong>
          <span class="pill">${r.is_admin ? 'Админ' : 'Пользователь'}</span>
        </div>
        <div class="credential-grid top-gap">
          <div><span class="muted">Пароль:</span> <code>${escapeHtml(r.password || 'недоступен')}</code></div>
          <div><span class="muted">Email:</span> ${escapeHtml(r.email || '')}</div>
          <div><span class="muted">API key:</span> <code>${escapeHtml(r.api_key || '')}</code></div>
          <div><span class="muted">Создан:</span> ${fmtDate(r.created_at)}</div>
        </div>
      </div>
    `).join('');
  }

  function downloadCredentialsTxt(){
    const orgId = $('#userOrgSelect')?.value;
    const query = orgId ? `?organization_id=${encodeURIComponent(orgId)}` : '';
    downloadProtectedFile(api(`/api/admin/user-credentials.txt${query}`), 'created_accounts.txt').catch(showError);
  }

  function selectedAnalyticsOrgId(){
    const sel = $('#analyticsOrgSelect');
    return sel && sel.value ? Number(sel.value) : null;
  }

  async function refreshAdminQuestions(){
    if(!me?.is_admin) return;
    const orgId = selectedAnalyticsOrgId();
    const query = orgId ? `?organization_id=${encodeURIComponent(orgId)}&limit=100` : '?limit=100';
    const data = await fetchJSON(`/api/admin/questions${query}`, {headers: authHeaders()});
    const items = Array.isArray(data.items) ? data.items : [];
    const summary = $('#questionsSummary');
    if(summary) summary.textContent = `Показано ${items.length} из ${data.total || items.length} вопросов`;
    const root = $('#adminQuestionsList');
    if(!root) return;
    if(!items.length){
      root.innerHTML = '<div class="item muted">В выбранной организации вопросов пока нет.</div>';
      return;
    }
    root.innerHTML = items.map((q) => `
      <article class="item question-item">
        <div class="row gap wrap">
          <div>
            <strong>${escapeHtml(q.question)}</strong>
            <div class="muted small top-gap">${escapeHtml(q.organization_name || 'Организация')} • ${escapeHtml(q.project_name || 'Проект')} • ${escapeHtml(q.user_login || q.user_label || 'Неизвестный пользователь')} • ${fmtDate(q.created_at)}</div>
          </div>
          <span class="pill">#${escapeHtml(String(q.id))}</span>
        </div>
        ${q.answer_preview ? `<div class="question-preview top-gap">${escapeHtml(q.answer_preview)}</div>` : ''}
      </article>
    `).join('');
  }

  function downloadAdminQuestionsCsv(){
    if(!me?.is_admin) return;
    const orgId = selectedAnalyticsOrgId();
    const query = orgId ? `?organization_id=${encodeURIComponent(orgId)}` : '';
    downloadProtectedFile(api(`/api/admin/questions.csv${query}`), 'organization_questions.csv').catch(showError);
  }

  async function initApp(){
    injectMarkdownStyles();
    $('#loginScreen').classList.add('hidden');
    $('#appShell').classList.remove('hidden');
    currentProjectId = null;
    chatState = [];
    await loadMe();
    await refreshProjects();
    setView('overviewView');
    await refreshCase10Shell();
    restoreChatState();
    renderChat();

    const pending = getPendingChat();
    if(pending?.job_id){
      setView('chatView');
      setStatus('Восстанавливаю ожидание ответа LLM…', 'warn');
      await resumePendingChatIfAny();
      return;
    }

    await refreshDocs();
    if(me.is_admin){ await refreshOrgs(); await refreshUsers(); await refreshAdminQuestions(); }
    setStatus('Готово');
  }


  document.addEventListener('submit', (e) => {
    e.preventDefault();
    e.stopPropagation();
    return false;
  }, true);

  $('#loginBtn').addEventListener('click', () => login().catch(showLoginError));
  $('#passwordInput').addEventListener('keydown', (e) => { if(e.key === 'Enter') login(); });
  $('#logoutBtn').addEventListener('click', logout);
  $('#refreshProjectsBtn').addEventListener('click', () => { if(!sendInFlight) refreshProjects().catch(showError); });
  $('#projectSelect').addEventListener('change', () => {
    if(sendInFlight){ $('#projectSelect').value = String(currentProjectId || ''); return; }
    currentProjectId = Number($('#projectSelect').value);
    selectedEntityId = null;
    restoreChatState();
    refreshCase10Shell().catch(showError);
    refreshDocs().catch(showError);
    renderChat();
    resumePendingChatIfAny().catch(() => {});
  });
  $('#createProjectBtn').addEventListener('click', () => createProject().catch(showError));
  $$('.nav-item').forEach(btn => btn.addEventListener('click', () => {
    setView(btn.dataset.view);
    if(btn.dataset.view==='docsView'){ if(!sendInFlight){ startDocsPolling(); refreshDocs().catch(showError); } } else { if(!sendInFlight) stopDocsPolling(); }
    if(btn.dataset.view==='overviewView' && !sendInFlight) refreshOverview().catch(showError);
    if(btn.dataset.view==='matrixView' && !sendInFlight) refreshMatrix().catch(showError);
    if(btn.dataset.view==='candidatesView' && !sendInFlight) refreshCandidates().catch(showError);
    if(btn.dataset.view==='objectsView' && !sendInFlight) refreshObjects().catch(showError);
    if(btn.dataset.view==='changesView' && !sendInFlight) refreshChanges().catch(showError);
    if(btn.dataset.view==='protocolView' && !sendInFlight) refreshProtocol().catch(showError);
    if(btn.dataset.view==='historyView' && !sendInFlight) refreshHistory().catch(showError);
    if(btn.dataset.view==='adminView' && !sendInFlight){ refreshOrgs().then(() => { refreshAdminQuestions().catch(showError); refreshCredentials().catch(showError); }).catch(showError); refreshUsers().catch(showError); }
  }));
  $('#startInspectionBtn')?.addEventListener('click', () => { if(!sendInFlight) startInspection().catch(showError); });
  $('#importOfficialDatasetBtn')?.addEventListener('click', () => { if(!sendInFlight) importOfficialDataset().catch(showError); });
  $('#seedSyntheticBtn')?.addEventListener('click', () => { if(!sendInFlight) seedSyntheticDataset().catch(showError); });
  $('#refreshMatrixBtn')?.addEventListener('click', () => { if(!sendInFlight) refreshMatrix().catch(showError); });
  $('#refreshCandidatesBtn')?.addEventListener('click', () => { if(!sendInFlight) refreshCandidates().catch(showError); });
  $('#refreshObjectsBtn')?.addEventListener('click', () => { if(!sendInFlight) refreshObjects().catch(showError); });
  $('#refreshChangesBtn')?.addEventListener('click', () => { if(!sendInFlight) refreshChanges().catch(showError); });
  $('#refreshProtocolBtn')?.addEventListener('click', () => { if(!sendInFlight) refreshProtocol().catch(showError); });
  $('#sendBtn').addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); sendQuestion().catch((err) => { addMessage('assistant', `### Ошибка\\n${err.message || err}`, [], [], [], [], []); }); });
  $('#questionInput')?.addEventListener('keydown', (e) => { if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); e.stopPropagation(); sendQuestion().catch((err) => { addMessage('assistant', `### Ошибка\\n${err.message || err}`, [], [], [], [], []); }); } });
  $('#clearChatBtn').addEventListener('click', (e) => { e.preventDefault(); if(sendInFlight) return; chatState=[]; clearSavedChatState(); setPendingChat(null); renderChat(); });
  $('#loadHistoryToChatBtn').addEventListener('click', () => { if(!sendInFlight) loadHistoryIntoChat().catch(showError); });
  $('#uploadBtn').addEventListener('click', (e) => { e.preventDefault(); if(!sendInFlight) uploadFile().catch(showError); });
  $('#refreshDocsBtn').addEventListener('click', () => { refreshDocs().catch(showError); });
  document.addEventListener('visibilitychange', () => { if(document.hidden) stopDocsPolling(); else if(!sendInFlight && !hasAnyPendingChat() && !$('#docsView')?.classList.contains('hidden')) startDocsPolling(); });
  $('#refreshHistoryBtn').addEventListener('click', () => { if(!sendInFlight) refreshHistory().catch(showError); });
  $('#createOrgBtn').addEventListener('click', () => createOrg().catch(showError));
  $('#refreshOrgsBtn').addEventListener('click', () => { if(!sendInFlight) refreshOrgs().catch(showError); });
  $('#createUserBtn').addEventListener('click', () => createUser().catch(showError));
  $('#createBatchBtn').addEventListener('click', () => createBatch().catch(showError));
  $('#refreshUsersBtn').addEventListener('click', () => { if(!sendInFlight){ refreshUsers().catch(showError); refreshCredentials().catch(showError); } });
  $('#refreshCredentialsBtn')?.addEventListener('click', () => { if(!sendInFlight) refreshCredentials().catch(showError); });
  $('#downloadCredentialsTxtBtn')?.addEventListener('click', () => { if(!sendInFlight) downloadCredentialsTxt(); });
  $('#userOrgSelect')?.addEventListener('change', () => { if(!sendInFlight){ refreshUsers().catch(showError); refreshCredentials().catch(showError); } });
  $('#refreshQuestionsBtn')?.addEventListener('click', () => { if(!sendInFlight) refreshAdminQuestions().catch(showError); });
  $('#downloadQuestionsCsvBtn')?.addEventListener('click', () => { if(!sendInFlight) downloadAdminQuestionsCsv(); });
  $('#analyticsOrgSelect')?.addEventListener('change', () => { if(!sendInFlight) refreshAdminQuestions().catch(showError); });

  document.addEventListener('click', (event) => {
    const closeTarget = event.target.closest('[data-close-lightbox="1"]');
    if(closeTarget){ closeImageLightbox(); return; }

    const entityBtn = event.target.closest('.js-select-entity');
    if(entityBtn){
      const entityId = entityBtn.dataset.entityId;
      if(entityId){
        selectedEntityId = entityId;
        setView('objectsView');
        refreshObjects({skipPortrait:true}).then(() => refreshPortrait(entityId)).catch(showError);
      }
      return;
    }

    const decisionBtn = event.target.closest('.js-issue-decision');
    if(decisionBtn){
      createIssueDecision(decisionBtn.dataset.issueId, decisionBtn.dataset.decision).catch(showError);
      return;
    }

    const evidencePageBtn = event.target.closest('.js-evidence-page');
    if(evidencePageBtn){
      openEvidencePage(evidencePageBtn).catch(error => setStatus(error.message, 'error'));
      return;
    }
    const evidenceDecisionBtn = event.target.closest('.js-evidence-decision');
    if(evidenceDecisionBtn){
      createEvidenceDecision(evidenceDecisionBtn.dataset.evidenceGroupId, evidenceDecisionBtn.dataset.decision).catch(showError);
      return;
    }

    const finalizeBtn = event.target.closest('.js-finalize-protocol');
    if(finalizeBtn){
      finalizeProtocol(finalizeBtn.dataset.protocolId).catch(showError);
      return;
    }

    const protocolPdfBtn = event.target.closest('.js-download-protocol-pdf');
    if(protocolPdfBtn){
      const protocolId = protocolPdfBtn.dataset.protocolId;
      if(protocolId) downloadProtectedFile(api(`/api/case10/protocols/${encodeURIComponent(protocolId)}/pdf`), `case10_protocol_${protocolId}.pdf`).catch(showError);
      return;
    }

    const syncSourcesBtn = event.target.closest('.js-sync-source-fragments');
    if(syncSourcesBtn){
      syncSourceFragments(syncSourcesBtn.dataset.documentVersionId).catch(showError);
      return;
    }

    const syncIfcBtn = event.target.closest('.js-sync-ifc-observations');
    if(syncIfcBtn){
      syncIfcObservations(syncIfcBtn.dataset.documentVersionId).catch(showError);
      return;
    }

    const img = event.target.closest('img.protected-region-image.clickable-image, img.protected-page-image.clickable-image');
    if(img && img.src){
      const card = img.closest('.asset-card, .source-asset-chip, .evidence-card, .pdf-viewer');
      const title = card?.querySelector('.evidence-card-title, .source-asset-chip-title, .pdf-viewer-title')?.textContent?.trim() || img.alt || 'Изображение';
      const sub = card?.querySelector('.evidence-card-sub, .source-asset-chip-sub, .pdf-viewer-sub')?.textContent?.trim() || '';
      openImageLightbox({src: img.src, caption: sub ? `${title} • ${sub}` : title});
      return;
    }

    const navBtn = event.target.closest('.js-viewer-nav');
    if(navBtn){
      const idx = Number(navBtn.dataset.chatIndex || -1);
      const direction = navBtn.dataset.direction === 'prev' ? -1 : 1;
      if(Number.isInteger(idx) && chatState[idx]){
        const pages = deriveViewerPages(chatState[idx]);
        const nextIndex = Math.max(0, Math.min((chatState[idx].viewer_index || 0) + direction, pages.length - 1));
        chatState[idx].viewer_index = nextIndex;
        renderChat();
      }
      return;
    }

    const dlPdf = event.target.closest('.js-download-pages-pdf');
    if(dlPdf){
      const ids = String(dlPdf.dataset.pageIds || '').split(',').map(v => Number(v)).filter(Boolean);
      if(ids.length){
        const filename = dlPdf.dataset.filename || 'selected_pages';
        downloadProtectedFile(buildPagesPdfUrl(ids), `${filename}_selected_pages.pdf`).catch(showError);
      }
      return;
    }

    const dl = event.target.closest('.js-download-page');
    if(dl){
      const pageId = Number(dl.dataset.pageId || 0);
      if(pageId){
        const filename = dl.dataset.filename || 'sheet';
        const pageNumber = dl.dataset.pageNumber || '';
        downloadProtectedFile(buildPageDownloadUrl(pageId), `${filename}_sheet_${pageNumber || '1'}.png`).catch(showError);
      }
    }
  });

  document.addEventListener('keydown', (event) => {
    if(event.key === 'Escape') closeImageLightbox();
    if(event.key === 'Enter' && (event.ctrlKey || event.metaKey) && document.activeElement === $('#questionInput')) sendQuestion().catch(showError);
  });
  window.addEventListener('focus', () => {
    if(token() && !sendInFlight && !hasAnyPendingChat()) refreshProjects().catch(() => {});
  });
  $('#lightboxCloseBtn')?.addEventListener('click', closeImageLightbox);

  if(token()) initApp().catch(() => { setToken(''); logout(); });
})();
