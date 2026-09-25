import { useState } from 'react';

export default function TopBar({ me, projects, currentProjectId, onSelectProject, onCreateProject, onLogout }) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);

  async function submitCreate(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      await onCreateProject(name.trim());
      setName('');
      setCreating(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="topbar">
      <div className="brand">CASE10 <span>Инспектор</span></div>

      <div className="project-picker">
        {projects.length > 0 && (
          <select
            className="select"
            value={currentProjectId || ''}
            onChange={(e) => onSelectProject(Number(e.target.value))}
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        )}
        {!creating && (
          <button className="btn small" type="button" onClick={() => setCreating(true)}>
            + Проект
          </button>
        )}
        {creating && (
          <form className="row" onSubmit={submitCreate}>
            <input
              className="input"
              autoFocus
              placeholder="Название проекта"
              value={name}
              onChange={(e) => setName(e.target.value)}
              style={{ width: 180 }}
            />
            <button className="btn small primary" type="submit" disabled={busy || !name.trim()}>Создать</button>
            <button className="btn small" type="button" onClick={() => setCreating(false)}>Отмена</button>
          </form>
        )}
      </div>

      <div className="spacer" />

      <div className="user-chip">
        <span>{me?.login}{me?.organization_name ? ` • ${me.organization_name}` : ''}</span>
        <button className="btn small" type="button" onClick={onLogout}>Выйти</button>
      </div>
    </div>
  );
}
