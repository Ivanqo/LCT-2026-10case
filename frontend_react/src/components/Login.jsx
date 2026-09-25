import { useState } from 'react';
import { api, setToken } from '../api.js';

export default function Login({ onLoggedIn }) {
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    if (!login.trim() || !password) {
      setError('Введите логин и пароль');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const data = await api.login(login.trim(), password);
      setToken(data.access_token);
      onLoggedIn(data.user);
    } catch (err) {
      setError(err.message || 'Ошибка входа');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={submit}>
        <h1>CASE10 — Инспектор</h1>
        <div className="sub">Камеральная проверка ПД / РД / ИД</div>
        <label className="field">
          <span>Логин</span>
          <input className="input" autoFocus value={login} onChange={(e) => setLogin(e.target.value)} autoComplete="username" />
        </label>
        <label className="field">
          <span>Пароль</span>
          <input
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        {error && <div className="error-banner"><span>{error}</span></div>}
        <button className="btn primary block" type="submit" disabled={busy}>
          {busy ? 'Выполняется вход…' : 'Войти'}
        </button>
      </form>
    </div>
  );
}
