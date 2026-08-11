import { FormEvent, useCallback, useEffect, useState } from 'react';

import { Mt5DemoConnectionPanel } from './Mt5DemoConnectionPanel';
import { TelegramConnectionPanel } from './TelegramConnectionPanel';
import { TelegramSourceSelector } from './TelegramSourceSelector';
import { TradingAdminOnboarding } from './TradingAdminOnboarding';

type AuthState = 'checking' | 'signed-out' | 'signed-in';
type Notice = { tone: 'error' | 'success'; message: string } | null;
type WorkspaceView = 'overview' | 'setup' | 'telegram' | 'sources' | 'mt5' | 'access';

interface AccessAction { permission: string; label: string; description: string; }
interface AccessSection { key: 'owner' | 'trading' | 'user'; label: string; description: string; actions: AccessAction[]; }
interface Account {
  id: string; email: string; display_name: string | null; role: 'owner' | 'trading_admin' | 'user'; role_label: string;
  roles: string[]; permissions: string[]; sections: AccessSection[];
  security: { two_factor: 'enabled' | 'setup_required'; passkey: 'enabled' | 'setup_available'; };
}
interface SharedSignalSource { source_id: string; chat_id: number; title: string; status: string; }
interface TelegramAccountSummary { id: string; status: string; }
interface TelegramSetupSource { selected: boolean; managed_by_this_reader?: boolean; }

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? '/api';

function workspaceViewFromHistory(value: unknown): WorkspaceView | null {
  return value === 'overview' || value === 'setup' || value === 'telegram' || value === 'sources' || value === 'mt5' || value === 'access' ? value : null;
}
function requestedWorkspaceView(): WorkspaceView | null {
  return workspaceViewFromHistory(new URLSearchParams(window.location.search).get('view'));
}
function historyStateWithView(view: WorkspaceView) {
  const existing = typeof window.history.state === 'object' && window.history.state !== null ? window.history.state : {};
  return { ...existing, superSignalsView: view };
}
function urlForView(view: WorkspaceView): string {
  const url = new URL(window.location.href);
  if (view === 'overview') url.searchParams.delete('view'); else url.searchParams.set('view', view);
  return `${url.pathname}${url.search}${url.hash}`;
}
async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? (body as { detail: unknown }).detail : 'Something went wrong.';
    const message = typeof detail === 'object' && detail !== null && 'message' in detail ? String((detail as { message: unknown }).message) : String(detail);
    throw new Error(message);
  }
  return body;
}
async function tradingAdminSetupComplete(account: Account): Promise<boolean> {
  if (account.role !== 'trading_admin' || !account.permissions.includes('sources.manage')) return true;
  const accountsResponse = await fetch(`${apiBaseUrl}/admin/telegram/accounts`, { credentials: 'include', headers: { Accept: 'application/json' } });
  const accounts = await readJson<TelegramAccountSummary[]>(accountsResponse);
  const connected = accounts.filter((item) => item.status === 'connected');
  if (connected.length === 0) return false;
  for (const telegramAccount of connected) {
    const sourcesResponse = await fetch(`${apiBaseUrl}/admin/telegram/sources/accounts/${telegramAccount.id}/available`, { credentials: 'include', headers: { Accept: 'application/json' } });
    const sources = await readJson<TelegramSetupSource[]>(sourcesResponse);
    if (sources.some((source) => source.selected && source.managed_by_this_reader !== false)) return true;
  }
  return false;
}

export function App() {
  const [authState, setAuthState] = useState<AuthState>('checking');
  const [account, setAccount] = useState<Account | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const [showRecovery, setShowRecovery] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeView, setActiveView] = useState<WorkspaceView>('overview');
  const [sharedSources, setSharedSources] = useState<SharedSignalSource[]>([]);
  const [sharedSourcesLoaded, setSharedSourcesLoaded] = useState(false);
  const [setupCheckComplete, setSetupCheckComplete] = useState(false);
  const canManageTelegram = account?.permissions.includes('sources.manage') ?? false;
  const canManageMt5 = account?.permissions.includes('mt5_accounts.approve') ?? false;
  const canViewOwnerAlerts = account?.permissions.includes('admins.manage') ?? false;

  const refreshSharedSources = useCallback(async () => {
    if (!canManageTelegram) { setSharedSources([]); setSharedSourcesLoaded(true); return; }
    try {
      const response = await fetch(`${apiBaseUrl}/admin/telegram/sources/shared`, { credentials: 'include', headers: { Accept: 'application/json' } });
      if (response.status === 404) { setSharedSources([]); setSharedSourcesLoaded(true); return; }
      const sources = await readJson<SharedSignalSource[]>(response);
      setSharedSources(Array.isArray(sources) ? sources : []);
    } catch { setSharedSources([]); }
    finally { setSharedSourcesLoaded(true); }
  }, [canManageTelegram]);

  useEffect(() => {
    const controller = new AbortController();
    async function restoreSession() {
      try {
        const response = await fetch(`${apiBaseUrl}/auth/me`, { credentials: 'include', headers: { Accept: 'application/json' }, signal: controller.signal });
        if (response.status === 401) { setAuthState('signed-out'); return; }
        setAccount(await readJson<Account>(response)); setAuthState('signed-in');
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setNotice({ tone: 'error', message: 'The secure service is unavailable.' }); setAuthState('signed-out');
      }
    }
    void restoreSession(); return () => controller.abort();
  }, []);

  useEffect(() => { if (authState === 'signed-in') void refreshSharedSources(); }, [account?.id, authState, refreshSharedSources]);

  useEffect(() => {
    if (authState !== 'signed-in' || !account) return;
    const currentAccount = account;
    let cancelled = false;
    setSetupCheckComplete(currentAccount.role !== 'trading_admin');
    async function checkSetup() {
      if (currentAccount.role !== 'trading_admin') return;
      try {
        const complete = await tradingAdminSetupComplete(currentAccount);
        if (cancelled) return;
        if (!complete) { setActiveView('setup'); window.history.replaceState(historyStateWithView('setup'), '', urlForView('setup')); }
      } catch {
        if (cancelled) return;
        setActiveView('setup'); window.history.replaceState(historyStateWithView('setup'), '', urlForView('setup'));
      } finally { if (!cancelled) setSetupCheckComplete(true); }
    }
    void checkSetup(); return () => { cancelled = true; };
  }, [account, authState]);

  useEffect(() => {
    if (authState !== 'signed-in') return;
    const requestedView = requestedWorkspaceView();
    const historyView = workspaceViewFromHistory(window.history.state?.superSignalsView);
    const candidate = historyView ?? requestedView ?? 'overview';
    const initialView = candidate === 'mt5' && !canManageMt5 ? 'overview' : candidate;
    setActiveView(initialView);
    window.history.replaceState(historyStateWithView(initialView), '', urlForView(initialView));
    function handlePopState(event: PopStateEvent) {
      const candidateView = workspaceViewFromHistory(event.state?.superSignalsView) ?? requestedWorkspaceView() ?? 'overview';
      const view = candidateView === 'mt5' && !canManageMt5 ? 'overview' : candidateView;
      setActiveView(view); setMenuOpen(false); if (view === 'overview') void refreshSharedSources();
    }
    window.addEventListener('popstate', handlePopState); return () => window.removeEventListener('popstate', handlePopState);
  }, [authState, canManageMt5, refreshSharedSources]);

  useEffect(() => { if (!menuOpen) return; const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setMenuOpen(false); }; window.addEventListener('keydown', closeOnEscape); return () => window.removeEventListener('keydown', closeOnEscape); }, [menuOpen]);

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setNotice(null); setSetupCheckComplete(false); const form = new FormData(event.currentTarget);
    try {
      const response = await fetch(`${apiBaseUrl}/auth/login`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ email: form.get('email'), password: form.get('password') }) });
      const authenticatedAccount = await readJson<Account>(response);
      const requestedView = requestedWorkspaceView();
      const loginView = requestedView === 'mt5' && !authenticatedAccount.permissions.includes('mt5_accounts.approve') ? 'overview' : requestedView ?? 'overview';
      setAccount(authenticatedAccount); setAuthState('signed-in'); setActiveView(loginView); window.history.replaceState(historyStateWithView(loginView), '', urlForView(loginView)); event.currentTarget.reset();
    } catch (error) { setNotice({ tone: 'error', message: error instanceof Error ? error.message : 'Login failed.' }); }
    finally { setBusy(false); }
  }

  async function handleLogout() {
    setBusy(true);
    try { await fetch(`${apiBaseUrl}/auth/logout`, { method: 'POST', credentials: 'include', headers: { Accept: 'application/json' } }); }
    finally { setAccount(null); setSharedSources([]); setSharedSourcesLoaded(false); setSetupCheckComplete(false); setAuthState('signed-out'); setNotice(null); setActiveView('overview'); setMenuOpen(false); window.history.replaceState({}, '', urlForView('overview')); setBusy(false); }
  }

  async function handleRecovery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setNotice(null); const form = new FormData(event.currentTarget);
    try { const response = await fetch(`${apiBaseUrl}/auth/recovery`, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ email: form.get('email') }) }); await readJson<{ message: string }>(response); }
    finally { setNotice({ tone: 'success', message: 'If the account is eligible, recovery instructions will be sent.' }); setBusy(false); }
  }

  function navigate(view: WorkspaceView) {
    if (view !== activeView) window.history.pushState(historyStateWithView(view), '', urlForView(view));
    setActiveView(view); setMenuOpen(false); if (view === 'overview') void refreshSharedSources();
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch { /* test environments */ }
  }

  if (authState === 'checking') return <main className="app-shell"><section className="auth-card auth-card--loading" aria-live="polite"><img className="brand-logo" src="/super-signals-logo.png" alt="Super Signals" /><p>Checking secure session…</p></section></main>;

  if (authState === 'signed-in' && account) {
    const displayName = account.display_name ?? account.role_label;
    if (account.role === 'trading_admin' && !setupCheckComplete) return <main className="app-shell"><section className="auth-card auth-card--loading" aria-live="polite"><img className="brand-logo" src="/super-signals-logo.png" alt="Super Signals" /><p>Checking your one-time Telegram setup…</p></section></main>;

    return <main className="app-shell workspace-shell"><section className="dashboard-card dashboard-card--workspace" aria-label="Super Signals workspace">
      <header className="workspace-topbar">
        <button className="workspace-brand workspace-brand-button" type="button" aria-label="Go to overview" onClick={() => navigate('overview')}><img className="brand-logo" src="/super-signals-logo.png" alt="Super Signals" /><span className="workspace-brand-copy"><strong>Trading workspace</strong><small>Private · controlled · live trading disabled</small></span></button>
        <div className="workspace-topbar-actions">{activeView !== 'overview' && <button className="topbar-home-button" type="button" onClick={() => navigate('overview')}><span aria-hidden="true">⌂</span><span>Overview</span></button>}<button className="menu-trigger" type="button" aria-label="Open menu" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}><span className="menu-bars" aria-hidden="true"><span /><span /><span /></span></button></div>
      </header>
      {menuOpen && <button className="menu-backdrop" type="button" aria-label="Close menu" onClick={() => setMenuOpen(false)} />}
      <aside className={`workspace-drawer ${menuOpen ? 'workspace-drawer--open' : ''}`} aria-label="Workspace navigation" aria-hidden={!menuOpen}>
        <div className="drawer-header"><div><strong>Super Signals</strong><span>{account.role_label}</span></div><button className="menu-close" type="button" aria-label="Close menu" onClick={() => setMenuOpen(false)}>×</button></div>
        <nav className="drawer-nav" aria-label="Main menu">
          <button type="button" aria-current={activeView === 'overview' ? 'page' : undefined} onClick={() => navigate('overview')}><span className="drawer-nav-icon" aria-hidden="true">01</span><span className="drawer-nav-label"><strong>Overview</strong><small>Workspace status and shortcuts</small></span></button>
          {account.role === 'trading_admin' && canManageTelegram && <button type="button" aria-current={activeView === 'setup' ? 'page' : undefined} onClick={() => navigate('setup')}><span className="drawer-nav-icon" aria-hidden="true">02</span><span className="drawer-nav-label"><strong>Telegram setup</strong><small>Connect once and add your groups</small></span></button>}
          {canManageTelegram && <button type="button" aria-current={activeView === 'telegram' ? 'page' : undefined} onClick={() => navigate('telegram')}><span className="drawer-nav-icon" aria-hidden="true">03</span><span className="drawer-nav-label"><strong>Telegram accounts</strong><small>Connect and manage private readers</small></span></button>}
          {canManageTelegram && <button type="button" aria-current={activeView === 'sources' ? 'page' : undefined} onClick={() => navigate('sources')}><span className="drawer-nav-icon" aria-hidden="true">04</span><span className="drawer-nav-label"><strong>Signal sources</strong><small>Add shared groups and channels</small></span></button>}
          {canManageMt5 && <button type="button" aria-current={activeView === 'mt5' ? 'page' : undefined} onClick={() => navigate('mt5')}><span className="drawer-nav-icon" aria-hidden="true">05</span><span className="drawer-nav-label"><strong>MT5 demo</strong><small>Vantage connection and Day 23 live state</small></span></button>}
          <button type="button" aria-current={activeView === 'access' ? 'page' : undefined} onClick={() => navigate('access')}><span className="drawer-nav-icon" aria-hidden="true">06</span><span className="drawer-nav-label"><strong>Access &amp; security</strong><small>Role, permissions and account security</small></span></button>
        </nav>
        <div className="drawer-footer"><div className="drawer-account"><strong>{displayName}</strong><small>{account.email}</small></div><button className="button button--quiet" type="button" onClick={handleLogout} disabled={busy}>Log out</button></div>
      </aside>
      <div className="workspace-content">
        {activeView !== 'overview' && activeView !== 'setup' && <button className="workspace-back-button" type="button" onClick={() => navigate('overview')}><span aria-hidden="true">←</span> Back to overview</button>}
        {activeView === 'setup' && account.role === 'trading_admin' && canManageTelegram && <TradingAdminOnboarding apiBaseUrl={apiBaseUrl} displayName={displayName} onComplete={() => { void refreshSharedSources(); navigate('overview'); }} />}
        {activeView === 'overview' && <section aria-labelledby="dashboard-title">
          <div className="overview-hero"><p className="eyebrow">{account.role_label}</p><h1 id="dashboard-title">Welcome back, {displayName}</h1><p className="intro">This is your operational dashboard. Detailed connection, source and security settings now live behind the menu instead of filling the homepage.</p><div className="overview-actions">{account.role === 'trading_admin' && canManageTelegram && <button className="button" type="button" onClick={() => navigate('setup')}>Add my Telegram groups</button>}{account.role !== 'trading_admin' && canManageTelegram && <button className="button" type="button" onClick={() => navigate('sources')}>Add a signal group</button>}{canManageMt5 && <button className="button" type="button" onClick={() => navigate('mt5')}>Open MT5 demo</button>}{canManageTelegram && <button className="button button--quiet" type="button" onClick={() => navigate('telegram')}>Telegram readers</button>}</div></div>
          <div className="overview-grid" aria-label="Workspace status"><article className="overview-card"><span className="status-label">Session</span><strong>Persistently signed in</strong><small>Your Super Signals session renews while you use the app.</small></article><article className="overview-card"><span className="status-label">Role</span><strong>{account.role_label}</strong><small>{account.permissions.length} approved permissions.</small></article><article className="overview-card"><span className="status-label">Trading state</span><strong>Live trading disabled</strong><small>Source operating states are separate from trade execution.</small></article></div>
          {canManageTelegram && <section className="overview-sources" aria-labelledby="overview-sources-title"><div className="overview-section-header"><div><span className="status-label">Signal network</span><h2 id="overview-sources-title">Connected signal sources</h2><p>Groups and channels currently shared across Super Signals, with each source showing its current operating state.</p></div><button className="button button--quiet" type="button" onClick={() => navigate('sources')}>Manage sources</button></div>{!sharedSourcesLoaded ? <p className="muted-copy">Loading connected sources…</p> : sharedSources.length === 0 ? <div className="overview-source-empty"><strong>No signal sources connected yet</strong><small>Add a Telegram group or channel to start building the shared signal network.</small></div> : <div className="overview-source-list" aria-label="Connected signal sources">{sharedSources.map((source) => <article className="overview-source-card" key={source.source_id}><div><span className={`connection-status connection-status--${source.status}`}>SHARED · {source.status.toUpperCase()}</span><h3>{source.title}</h3><small>Connected to the shared Super Signals source catalogue</small></div><button className="button button--quiet" type="button" onClick={() => navigate('sources')}>Manage</button></article>)}</div>}</section>}
        </section>}
        {activeView === 'telegram' && canManageTelegram && <section className="workspace-panel-page" aria-labelledby="telegram-page-title"><div className="workspace-page-header"><div><p className="eyebrow">Private reader connections</p><h1 id="telegram-page-title">Telegram accounts</h1><p className="intro">Each connected reader remains private to the administrator who authorised it.</p></div><span className="workspace-role-pill">Private sessions</span></div><TelegramConnectionPanel apiBaseUrl={apiBaseUrl} /></section>}
        {activeView === 'sources' && canManageTelegram && <section className="workspace-panel-page" aria-labelledby="sources-page-title"><div className="workspace-page-header"><div><p className="eyebrow">Shared Super Signals data</p><h1 id="sources-page-title">Signal sources</h1><p className="intro">Add shared Telegram groups or channels and control each source as Testing, Live or Paused. LIVE is a source state only and does not enable trade execution.</p></div><span className="workspace-role-pill">STATE CONTROL</span></div><TelegramSourceSelector apiBaseUrl={apiBaseUrl} canViewOwnerAlerts={canViewOwnerAlerts} /></section>}
        {activeView === 'mt5' && canManageMt5 && <section className="workspace-panel-page" aria-labelledby="mt5-page-title"><div className="workspace-page-header"><div><p className="eyebrow">Owner broker connection</p><h1 id="mt5-page-title">MT5 demo</h1><p className="intro">Vantage demo connection and the Day 23 account-state / XAUUSD read gate.</p></div><span className="workspace-role-pill">DAY 23</span></div><Mt5DemoConnectionPanel apiBaseUrl={apiBaseUrl} /></section>}
        {activeView === 'access' && <section className="access-page" aria-labelledby="access-page-title"><div className="workspace-page-header"><div><p className="eyebrow">Account controls</p><h1 id="access-page-title">Access &amp; security</h1><p className="intro">Your role, security state and approved workspace permissions.</p></div><span className="workspace-role-pill">{account.role_label}</span></div><div className="status-grid"><article className="status-card status-card--healthy"><span className="status-label">Session</span><strong>Persistently signed in</strong><small>{account.email}</small></article><article className="status-card"><span className="status-label">Role</span><strong>{account.role_label}</strong><small>{account.permissions.length} approved permissions</small></article><article className="status-card"><span className="status-label">Additional security</span><strong>{account.security.two_factor === 'enabled' ? '2FA enabled' : 'Setup required'}</strong><small>{account.security.passkey === 'enabled' ? 'Passkey enabled' : 'Passkey setup available soon'}</small></article></div><div className="access-grid" aria-label="Approved workspace areas">{account.sections.map((section) => <article className={`access-section access-section--${section.key}`} key={section.key}><span className="status-label">{section.label}</span><p>{section.description}</p><ul className="access-action-list">{section.actions.map((action) => <li key={action.permission}><strong>{action.label}</strong><small>{action.description}</small></li>)}</ul></article>)}</div><div className="foundation-note"><span className="pulse" aria-hidden="true" />Role restrictions are enforced by the API and every denial is audited.</div></section>}
      </div>
    </section></main>;
  }

  return <main className="app-shell"><section className="auth-card" aria-labelledby="login-title"><img className="brand-logo" src="/super-signals-logo.png" alt="Super Signals" /><p className="eyebrow">Private account access</p><h1 id="login-title">{showRecovery ? 'Recover access' : 'Sign in securely'}</h1><p className="intro">{showRecovery ? 'Enter the account email. The response will never reveal whether an account exists.' : 'Sign in once on this device. Super Signals keeps the session active while you use the app.'}</p>{notice && <div className={`notice notice--${notice.tone}`} role="status">{notice.message}</div>}{showRecovery ? <form className="auth-form" onSubmit={handleRecovery}><label>Account email<input name="email" type="email" autoComplete="email" required /></label><button className="button" type="submit" disabled={busy}>{busy ? 'Submitting…' : 'Send recovery instructions'}</button><button className="text-button" type="button" onClick={() => { setShowRecovery(false); setNotice(null); }}>Back to sign in</button></form> : <form className="auth-form" onSubmit={handleLogin}><label>Email<input name="email" type="email" autoComplete="username" required /></label><label>Password<input name="password" type="password" autoComplete="current-password" required /></label><button className="button" type="submit" disabled={busy}>{busy ? 'Checking…' : 'Sign in'}</button><button className="text-button" type="button" onClick={() => { setShowRecovery(true); setNotice(null); }}>I cannot access my account</button></form>}</section></main>;
}
