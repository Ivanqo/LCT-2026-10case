const TOKEN_KEY = 'case10_react_token';

export const API_BASE = (() => {
  const qs = new URLSearchParams(window.location.search);
  if (qs.get('api')) return qs.get('api').replace(/\/$/, '');
  if (import.meta.env.VITE_API_BASE) return import.meta.env.VITE_API_BASE.replace(/\/$/, '');
  return `${window.location.protocol}//${window.location.hostname}:8080`;
})();

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(value) {
  if (value) localStorage.setItem(TOKEN_KEY, value);
  else localStorage.removeItem(TOKEN_KEY);
}

function authHeaders(extra) {
  const headers = { ...(extra || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, opts = {}) {
  const { method = 'GET', body, headers } = opts;
  const finalHeaders = authHeaders(headers);
  if (body !== undefined && !(body instanceof FormData)) {
    finalHeaders['Content-Type'] = 'application/json';
  }
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: finalHeaders,
      body: body instanceof FormData ? body : body !== undefined ? JSON.stringify(body) : undefined,
      cache: 'no-store',
    });
  } catch (err) {
    throw new ApiError(`Нет соединения с сервером (${API_BASE}). ${err.message || err}`, 0);
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* not JSON */
  }
  if (!res.ok) {
    const detail = (data && (data.detail || data.error?.message)) || text || `HTTP ${res.status}`;
    throw new ApiError(typeof detail === 'string' ? detail : JSON.stringify(detail), res.status);
  }
  return data;
}

const openCalls = new Map();

export const api = {
  login: (login, password) => request('/api/auth/login', { method: 'POST', body: { login, password } }),
  logout: () => request('/api/auth/logout', { method: 'POST' }),
  me: () => request('/api/me'),

  listProjects: () => request('/api/projects'),
  createProject: (name, description) => request('/api/projects', { method: 'POST', body: { name, description: description || null } }),

  listCase10Documents: (projectId) => request(`/api/case10/documents?project_id=${projectId}`),
  deleteDocument: (id, projectId, sourceType) =>
    request(`/api/documents/${id}?project_id=${projectId}&source_type=${encodeURIComponent(sourceType || 'rag')}`, { method: 'DELETE' }),

  createProcess: (projectId, body) => request(`/api/case10/projects/${projectId}/processes`, { method: 'POST', body: body || {} }),
  listProcesses: (projectId) => request(`/api/case10/processes?project_id=${projectId}`),
  processStatus: (processId) => request(`/api/case10/processes/${processId}/status`),
  runProcess: (processId) => request(`/api/case10/processes/${processId}/run`, { method: 'POST' }),

  currentProtocol: (projectId, processId) =>
    request(`/api/case10/protocols/current?project_id=${projectId}${processId ? `&process_id=${processId}` : ''}`),
  finalizeProtocol: (protocolId) => request(`/api/case10/protocols/${protocolId}/finalize`, { method: 'POST' }),
  unfinalizeProtocol: (protocolId, reason) =>
    request(`/api/case10/protocols/${protocolId}/unfinalize`, { method: 'POST', body: { reason } }),

  evidenceDecision: (evidenceGroupId, decision, reasonCode, comment, uiMetrics) =>
    request(`/api/case10/evidence-groups/${evidenceGroupId}/decisions`, {
      method: 'POST',
      body: { decision, reason_code: reasonCode || null, comment: comment || null, ui_metrics: uiMetrics || null },
    }),

  // --- S5 inspector workbench ---
  workbench: (processId) => request(`/api/case10/processes/${processId}/workbench`),
  groupWorkbench: (groupId) => request(`/api/case10/evidence-groups/${groupId}/workbench`),
  addFragment: (groupId, body) => request(`/api/case10/evidence-groups/${groupId}/fragments`, { method: 'POST', body }),
  refineFragment: (groupId, key, body) =>
    request(`/api/case10/evidence-groups/${groupId}/fragments/${encodeURIComponent(key)}/refine`, { method: 'POST', body }),
  removeFragment: (groupId, key, reason) =>
    request(`/api/case10/evidence-groups/${groupId}/fragments/${encodeURIComponent(key)}/remove`, { method: 'POST', body: { reason } }),
  restoreFragment: (groupId, key, reason) =>
    request(`/api/case10/evidence-groups/${groupId}/fragments/${encodeURIComponent(key)}/restore`, { method: 'POST', body: { reason } }),
  bulkDecisions: (body) => request('/api/case10/evidence-groups/bulk-decisions', { method: 'POST', body }),
  revisions: (processId) => request(`/api/case10/processes/${processId}/revisions`),
  chooseRevision: (processId, body) => request(`/api/case10/processes/${processId}/revision-choices`, { method: 'POST', body }),
  completeness: (processId) => request(`/api/case10/processes/${processId}/completeness`),
  verificationOpen: (processId) => {
    // One in-flight call per process: a double-mounted effect (React StrictMode) must not start two clocks.
    if (!openCalls.has(processId)) {
      const call = request(`/api/case10/processes/${processId}/verification/open`, { method: 'POST' });
      openCalls.set(processId, call);
      call.finally(() => openCalls.delete(processId)).catch(() => {});
    }
    return openCalls.get(processId);
  },
  verificationTiming: (processId) => request(`/api/case10/processes/${processId}/verification/timing`),
  pageGeometry: (docId, page) => request(`/api/case10/document-versions/${docId}/pages/${page}/geometry`),
};

const pageImageCache = new Map();

/** A page of the original (no highlight) as a blob URL; cached per document/page for the session. */
export function fetchPageImage(docId, page, maxSide = 1800) {
  const key = `${docId}:${page}:${maxSide}`;
  if (!pageImageCache.has(key)) {
    const promise = fetch(`${API_BASE}/api/case10/document-versions/${docId}/pages/${page}.png?max_side=${maxSide}`, {
      headers: authHeaders(),
    }).then(async (res) => {
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const data = await res.json();
          detail = data.detail || detail;
        } catch {
          /* ignore */
        }
        throw new ApiError(detail, res.status);
      }
      return URL.createObjectURL(await res.blob());
    });
    promise.catch(() => pageImageCache.delete(key));
    pageImageCache.set(key, promise);
  }
  return pageImageCache.get(key);
}

/** Go to source: the original page (vector PDF, text layer) opened in a new browser tab. */
export async function openSourcePage(docId, page) {
  const win = window.open('', '_blank');
  try {
    const res = await fetch(`${API_BASE}/api/case10/document-versions/${docId}/pages/${page}.pdf`, { headers: authHeaders() });
    if (!res.ok) throw new ApiError(`Не удалось открыть источник (HTTP ${res.status})`, res.status);
    const url = URL.createObjectURL(await res.blob());
    if (win) win.location.href = url;
    else window.open(url, '_blank');
  } catch (err) {
    if (win) win.close();
    throw err;
  }
}

export function uploadFile(projectId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('project_id', String(projectId));
    form.append('file_type', 'auto');
    form.append('file', file);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE}/api/upload`);
    xhr.responseType = 'json';
    const token = getToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable || !onProgress) return;
      onProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response);
      } else {
        const detail = xhr.response?.detail || `HTTP ${xhr.status}`;
        reject(new ApiError(typeof detail === 'string' ? detail : JSON.stringify(detail), xhr.status));
      }
    };
    xhr.onerror = () => reject(new ApiError('Сбой сети при загрузке файла', 0));
    xhr.send(form);
  });
}

export async function fetchEvidencePageImage(fragmentId) {
  const res = await fetch(`${API_BASE}/api/case10/evidence-fragments/${fragmentId}/page.png`, {
    headers: authHeaders(),
    cache: 'no-store',
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, res.status);
  }
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

export function protocolPdfUrl(protocolId) {
  return `${API_BASE}/api/case10/protocols/${protocolId}/pdf`;
}

export function protocolSubmissionUrl(protocolId) {
  return `${API_BASE}/api/case10/protocols/${protocolId}/submission`;
}

export async function downloadProtectedFile(url, filename) {
  const res = await fetch(url, { headers: authHeaders(), cache: 'no-store' });
  if (!res.ok) throw new ApiError(`Не удалось скачать файл (HTTP ${res.status})`, res.status);
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = filename || 'file';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}
