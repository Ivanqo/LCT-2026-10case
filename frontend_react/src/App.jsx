import { useEffect, useState } from 'react';
import Login from './components/Login.jsx';
import TopBar from './components/TopBar.jsx';
import DocumentsPanel from './components/DocumentsPanel.jsx';
import ProcessPanel from './components/ProcessPanel.jsx';
import { api, getToken, setToken } from './api.js';

const PROJECT_KEY = 'case10_react_project_id';
const PROCESS_MAP_KEY = 'case10_react_process_map';

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
  const [view, setView] = useState('documents');
  const [error, setError] = useState('');

  useEffect(() => {
    async function boot() {
      if (!getToken()) {
        setBooting(false);
        return;
      }
      try {
        const user = await api.me();
        setMe(user);
        await loadProjects();
      } catch {
        setToken('');
      } finally {
        setBooting(false);
      }
    }
    boot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    selectProject(resolved);
  }

  function selectProject(id) {
    setProjectId(id);
    localStorage.setItem(PROJECT_KEY, String(id));
    const map = loadProcessMap();
    setProcessId(map[id] || null);
    setView('documents');
  }

  function handleProcessIdChange(id) {
    setProcessId(id);
    if (projectId) {
      const map = loadProcessMap();
      if (id) map[projectId] = id;
      else delete map[projectId];
      saveProcessMap(map);
    }
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

  if (booting) {
    return <div className="auth-shell"><span className="spinner" /></div>;
  }

  if (!me) {
    return <Login onLoggedIn={handleLoggedIn} />;
  }

  return (
    <div className="app-shell">
      <TopBar
        me={me}
        projects={projects}
        currentProjectId={projectId}
        onSelectProject={selectProject}
        onCreateProject={handleCreateProject}
        onLogout={handleLogout}
      />

      {projects.length === 0 ? (
        <div className="content">
          <div className="card empty-state">У вас пока нет проектов. Создайте проект, чтобы начать проверку CASE10.</div>
        </div>
      ) : (
        <>
          <div className="tabs">
            <button className={`tab${view === 'documents' ? ' active' : ''}`} type="button" onClick={() => setView('documents')}>
              Документы
            </button>
            <button className={`tab${view === 'process' ? ' active' : ''}`} type="button" onClick={() => setView('process')}>
              Проверка{processId ? '' : ' (не запущена)'}
            </button>
          </div>

          <div className={`content${view === 'process' ? ' wide' : ''}`}>
            {error && <div className="error-banner"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
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
