import { useEffect, useState } from 'react';
import Login from './components/Login.jsx';
import TopBar from './components/TopBar.jsx';
import DocumentsPanel from './components/DocumentsPanel.jsx';
import ProcessPanel from './components/ProcessPanel.jsx';
import Dashboard from './components/Dashboard.jsx';
import MlReport from './components/MlReport.jsx';
import NormativeBase from './components/NormativeBase.jsx';
import { api, getToken, setToken } from './api.js';

const PROJECT_KEY = 'case10_react_project_id';
const PROCESS_MAP_KEY = 'case10_react_process_map';
const VIEW_KEY = 'case10_react_view';
const VIEWS = new Set(['dashboard', 'documents', 'process', 'ml', 'normative']);

function loadView() {
  try {
    const stored = sessionStorage.getItem(VIEW_KEY);
    return VIEWS.has(stored) ? stored : 'documents';
  } catch {
    return 'documents';
  }
}

function loadProcessMap() {
  try {
    return JSON.parse(localStorage.getItem(PROCESS_MAP_KEY) || '{}');
  } catch {
    return {};
  }
}

function saveProcessMap(map) {
  try {
    localStorage.setItem(PROCESS_MAP_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

export default function App() {
  const [booting, setBooting] = useState(true);
  const [me, setMe] = useState(null);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(null);
  const [processId, setProcessId] = useState(null);
  const [view, setViewState] = useState(loadView);
  const setView = (next) => {
    setViewState(next);
    try { sessionStorage.setItem(VIEW_KEY, next); } catch { /* storage unavailable: the tab is simply not remembered */ }
  };
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [bootError, setBootError] = useState('');

  async function boot() {
    setBootError('');
    if (!getToken()) {
      setBooting(false);
      return;
    }
    setBooting(true);
    try {
      const user = await api.me();
      setMe(user);
      await loadProjects();
    } catch (err) {
      // an unreachable server must not log the inspector out: keep the token and offer a retry
      if (err.status === 0 || err.status >= 500) setBootError(err.message || 'Сервер недоступен');
      else setToken('');
    } finally {
      setBooting(false);
    }
  }

  useEffect(() => {
    boot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    function onExpired() {
      setMe(null);
      setProjects([]);
      setProjectId(null);
      setProcessId(null);
      setNotice('Сессия истекла — войдите снова. Выбранная проверка сохранена.');
    }
    window.addEventListener('case10:auth-expired', onExpired);
    return () => window.removeEventListener('case10:auth-expired', onExpired);
  }, []);

  async function loadProjects(preferredId) {
    const rows = await api.listProjects();
    setProjects(rows);
    if (!rows.length) {
      setProjectId(null);
      return;
    }
    const stored = preferredId || Number(localStorage.getItem(PROJECT_KEY));
    const resolved = rows.some((p) => p.id === stored) ? stored : rows[0].id;
    selectProject(resolved, true);
  }

  function selectProject(id, keepView = false) {
    setProjectId(id);
    localStorage.setItem(PROJECT_KEY, String(id));
    const map = loadProcessMap();
    setProcessId(map[id] || null);
    if (!keepView) setView('documents');
  }

  // A browser that has no remembered check for this project still lands on its latest one, so the tab does not claim
  // "not started" before the check list has been opened.
  useEffect(() => {
    if (!projectId || processId) return undefined;
    let active = true;
    api.listProcesses(projectId)
      .then((rows) => { if (active && rows.length) handleProcessIdChange(rows[0].process_id); })
      .catch(() => {});
    return () => { active = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, processId]);

  function handleProcessIdChange(id) {
    setProcessId(id);
    if (projectId) {
      const map = loadProcessMap();
      if (id) map[projectId] = id;
      else delete map[projectId];
      saveProcessMap(map);
    }
  }

  // Dashboard click: open the check's workplace (its project and process) without going through the documents tab.
  function openFromDashboard(targetProjectId, targetProcessId) {
    const map = loadProcessMap();
    map[targetProjectId] = targetProcessId;
    saveProcessMap(map);
    setProjectId(targetProjectId);
    localStorage.setItem(PROJECT_KEY, String(targetProjectId));
    setProcessId(targetProcessId);
    setView('process');
  }

  function handleProcessStarted(id) {
    handleProcessIdChange(id);
    setView('process');
  }

  async function handleCreateProject(name) {
    try {
      const created = await api.createProject(name);
      await loadProjects(created.id);
    } catch (err) {
      setError(err.message || 'Не удалось создать проект');
      throw err;
    }
  }

  async function handleLoggedIn(user) {
    setMe(user);
    setNotice('');
    setError('');
    try {
      await loadProjects();
    } catch (err) {
      setError(err.message || 'Не удалось загрузить проекты');
    }
  }

  function handleLogout() {
    api.logout().catch(() => {});
    setToken('');
    setMe(null);
    setProjects([]);
    setProjectId(null);
    setProcessId(null);
  }

  const canSeeMlReport = Boolean(me?.is_admin || ['ADMIN', 'ML_ENGINEER'].includes(String(me?.role || '').toUpperCase()));
  const canManageNormative = Boolean(me?.is_admin || String(me?.role || '').toUpperCase() === 'ADMIN');

  if (booting) {
    return <div className="auth-shell"><span className="spinner" /></div>;
  }

  if (bootError) {
    return (
      <div className="auth-shell">
        <div className="auth-card">
          <h1>Сервер недоступен</h1>
          <div className="error-banner" role="alert"><span>{bootError}</span></div>
          <button className="btn primary block" type="button" onClick={boot}>Повторить</button>
        </div>
      </div>
    );
  }

  if (!me) {
    return <Login onLoggedIn={handleLoggedIn} notice={notice} />;
  }

  return (
    <div className="app-shell">
      <TopBar
        me={me}
        projects={projects}
        currentProjectId={projectId}
        onSelectProject={(id) => selectProject(id)}
        onCreateProject={handleCreateProject}
        onLogout={handleLogout}
      />

      {projects.length === 0 ? (
        <div className="content">
          {canManageNormative && (
            <div className="tabs">
              <button className={`tab${view === 'normative' ? ' active' : ''}`} type="button" onClick={() => setView('normative')}>
                Нормативная база
              </button>
            </div>
          )}
          {view === 'normative' && canManageNormative
            ? <NormativeBase />
            : <div className="card empty-state">У вас пока нет проектов. Создайте проект, чтобы начать проверку CASE10.</div>}
        </div>
      ) : (
        <>
          <div className="tabs">
            <button className={`tab${view === 'dashboard' ? ' active' : ''}`} type="button" onClick={() => setView('dashboard')}>
              Панель объектов
            </button>
            <button className={`tab${view === 'documents' ? ' active' : ''}`} type="button" onClick={() => setView('documents')}>
              Документы
            </button>
            <button className={`tab${view === 'process' ? ' active' : ''}`} type="button" onClick={() => setView('process')}>
              Проверка{processId ? '' : ' (не запущена)'}
            </button>
            {canSeeMlReport && (
              <button className={`tab${view === 'ml' ? ' active' : ''}`} type="button" onClick={() => setView('ml')}>
                Отчёт ML
              </button>
            )}
            {canManageNormative && (
              <button className={`tab${view === 'normative' ? ' active' : ''}`} type="button" onClick={() => setView('normative')}>
                Нормативная база
              </button>
            )}
          </div>

          <div className={`content${view === 'process' || view === 'dashboard' ? ' wide' : ''}`}>
            {error && <div className="error-banner"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
            {view === 'dashboard' && <Dashboard projectId={projectId} onOpen={openFromDashboard} />}
            {view === 'ml' && canSeeMlReport && <MlReport projectId={projectId} />}
            {view === 'normative' && canManageNormative && <NormativeBase />}
            {view === 'documents' && (
              <DocumentsPanel projectId={projectId} onProcessStarted={handleProcessStarted} />
            )}
            {view === 'process' && (
              <ProcessPanel projectId={projectId} processId={processId} onProcessIdChange={handleProcessIdChange} me={me} />
            )}
          </div>
        </>
      )}
    </div>
  );
}
