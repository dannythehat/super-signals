import { FormEvent, useEffect, useState } from 'react';

type AuthState = 'checking' | 'signed-out' | 'signed-in';
type Notice = { tone: 'error' | 'success'; message: string } | null;

interface AccessAction {
  permission: string;
  label: string;
  description: string;
}

interface AccessSection {
  key: 'owner' | 'trading' | 'user';
  label: string;
  description: string;
  actions: AccessAction[];
}

interface Account {
  id: string;
  email: string;
  display_name: string | null;
  role: 'owner' | 'trading_admin' | 'user';
  role_label: string;
  roles: string[];
  permissions: string[];
  sections: AccessSection[];
  security: {
    two_factor: 'enabled' | 'setup_required';
    passkey: 'enabled' | 'setup_available';
  };
}

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? '/api';

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : 'Something went wrong.';
    const message =
      typeof detail === 'object' && detail !== null && 'message' in detail
        ? String((detail as { message: unknown }).message)
        : String(detail);
    throw new Error(message);
  }
  return body;
}

export function App() {
  const [authState, setAuthState] = useState<AuthState>('checking');
  const [account, setAccount] = useState<Account | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const [showRecovery, setShowRecovery] = useState(false);

  useEffect(() => {
    const controller = new AbortController();

    async function restoreSession() {
      try {
        const response = await fetch(`${apiBaseUrl}/auth/me`, {
          credentials: 'include',
          headers: { Accept: 'application/json' },
          signal: controller.signal,
        });
        if (response.status === 401) {
          setAuthState('signed-out');
          return;
        }
        setAccount(await readJson<Account>(response));
        setAuthState('signed-in');
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setNotice({ tone: 'error', message: 'The secure service is unavailable.' });
        setAuthState('signed-out');
      }
    }

    void restoreSession();
    return () => controller.abort();
  }, []);

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    const form = new FormData(event.currentTarget);

    try {
      const response = await fetch(`${apiBaseUrl}/auth/login`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          email: form.get('email'),
          password: form.get('password'),
        }),
      });
      const authenticatedAccount = await readJson<Account>(response);
      setAccount(authenticatedAccount);
      setAuthState('signed-in');
      event.currentTarget.reset();
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Login failed.',
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleLogout() {
    setBusy(true);
    try {
      await fetch(`${apiBaseUrl}/auth/logout`, {
        method: 'POST',
        credentials: 'include',
        headers: { Accept: 'application/json' },
      });
    } finally {
      setAccount(null);
      setAuthState('signed-out');
      setNotice(null);
      setBusy(false);
    }
  }

  async function handleRecovery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    const form = new FormData(event.currentTarget);

    try {
      const response = await fetch(`${apiBaseUrl}/auth/recovery`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ email: form.get('email') }),
      });
      await readJson<{ message: string }>(response);
      setNotice({
        tone: 'success',
        message: 'If the account is eligible, recovery instructions will be sent.',
      });
    } catch {
      setNotice({
        tone: 'success',
        message: 'If the account is eligible, recovery instructions will be sent.',
      });
    } finally {
      setBusy(false);
    }
  }

  if (authState === 'checking') {
    return (
      <main className="app-shell">
        <section className="auth-card auth-card--loading" aria-live="polite">
          <img className="brand-logo" src="/super-signals-logo.png" alt="Super Signals" />
          <p>Checking secure session…</p>
        </section>
      </main>
    );
  }

  if (authState === 'signed-in' && account) {
    return (
      <main className="app-shell">
        <section className="dashboard-card" aria-labelledby="dashboard-title">
          <header className="dashboard-header">
            <img
              className="brand-logo brand-logo--dashboard"
              src="/super-signals-logo.png"
              alt=""
            />
            <button
              className="button button--quiet"
              type="button"
              onClick={handleLogout}
              disabled={busy}
            >
              Log out
            </button>
          </header>

          <p className="eyebrow">{account.role_label}</p>
          <h1 id="dashboard-title">Welcome, {account.display_name ?? account.role_label}</h1>
          <p className="intro">Your workspace shows only the controls approved for this account.</p>

          <div className="status-grid">
            <article className="status-card status-card--healthy">
              <span className="status-label">Session</span>
              <strong>Securely signed in</strong>
              <small>{account.email}</small>
            </article>
            <article className="status-card">
              <span className="status-label">Role</span>
              <strong>{account.role_label}</strong>
              <small>{account.permissions.length} approved permissions</small>
            </article>
            <article className="status-card">
              <span className="status-label">Additional security</span>
              <strong>
                {account.security.two_factor === 'enabled' ? '2FA enabled' : 'Setup required'}
              </strong>
              <small>
                {account.security.passkey === 'enabled'
                  ? 'Passkey enabled'
                  : 'Passkey setup available soon'}
              </small>
            </article>
          </div>

          <div className="access-grid" aria-label="Approved workspace areas">
            {account.sections.map((section) => (
              <article
                className={`access-section access-section--${section.key}`}
                key={section.key}
              >
                <span className="status-label">{section.label}</span>
                <p>{section.description}</p>
                <ul className="access-action-list">
                  {section.actions.map((action) => (
                    <li key={action.permission}>
                      <strong>{action.label}</strong>
                      <small>{action.description}</small>
                    </li>
                  ))}
                </ul>
              </article>
            ))}
          </div>

          <div className="foundation-note">
            <span className="pulse" aria-hidden="true" />
            Role restrictions are enforced by the API and every denial is audited. No live trading
            is enabled.
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <section className="auth-card" aria-labelledby="login-title">
        <img className="brand-logo" src="/super-signals-logo.png" alt="Super Signals" />
        <p className="eyebrow">Private account access</p>
        <h1 id="login-title">{showRecovery ? 'Recover access' : 'Sign in securely'}</h1>
        <p className="intro">
          {showRecovery
            ? 'Enter the account email. The response will never reveal whether an account exists.'
            : 'Only approved Super Signals accounts can continue.'}
        </p>

        {notice && (
          <div className={`notice notice--${notice.tone}`} role="status">
            {notice.message}
          </div>
        )}

        {showRecovery ? (
          <form className="auth-form" onSubmit={handleRecovery}>
            <label>
              Account email
              <input name="email" type="email" autoComplete="email" required />
            </label>
            <button className="button" type="submit" disabled={busy}>
              {busy ? 'Submitting…' : 'Send recovery instructions'}
            </button>
            <button
              className="text-button"
              type="button"
              onClick={() => {
                setShowRecovery(false);
                setNotice(null);
              }}
            >
              Back to sign in
            </button>
          </form>
        ) : (
          <form className="auth-form" onSubmit={handleLogin}>
            <label>
              Email
              <input name="email" type="email" autoComplete="username" required />
            </label>
            <label>
              Password
              <input name="password" type="password" autoComplete="current-password" required />
            </label>
            <button className="button" type="submit" disabled={busy}>
              {busy ? 'Checking…' : 'Sign in'}
            </button>
            <button
              className="text-button"
              type="button"
              onClick={() => {
                setShowRecovery(true);
                setNotice(null);
              }}
            >
              I cannot access my account
            </button>
          </form>
        )}
      </section>
    </main>
  );
}
