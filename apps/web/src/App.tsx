import { FormEvent, useCallback, useEffect, useState } from 'react';

import { MobileDashboard } from './MobileDashboard';
import { Mt5DemoConnectionPanel } from './Mt5DemoConnectionPanel';
import { PushNotificationsDay34 } from './PushNotificationsDay34';
import { TelegramConnectionPanel } from './TelegramConnectionPanel';
import { TelegramSourceSelector } from './TelegramSourceSelector';
import { TradingActivationPanel } from './TradingActivationPanel';
import { TradingAdminOnboarding } from './TradingAdminOnboarding';
import { UserMt5ConnectionPanel } from './UserMt5ConnectionPanel';

type AuthState = 'checking' | 'signed-out' | 'signed-in';
type Notice = { tone: 'error' | 'success'; message: string } | null;
type WorkspaceView = 'overview' | 'settings' | 'setup' | 'telegram' | 'sources' | 'mt5' | 'access';

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
const SESSION_RETRY_MS = 1500;
const SERVICE_RECONNECT_MESSAGE = 'Reconnecting to the secure service…';
const SERVICE_RESPONSE_ERROR = 'The secure service is reconnecting. Please try again in a moment.';

function workspaceViewFromHistory(value: unknown): WorkspaceView | null {
  return value === 'overview' || value === 'settings' || value === 'setup' || value === 'telegram' || value === 'sources' || value === 'mt5' || value === 'access' ? value : null;
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
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
  if (!contentType.includes('application/json')) throw new Error(SERVICE_RESPONSE_ERROR);

  let body: T;
  try {
    body = (await response.json()) as T;
  } catch {
    throw new Error(SERVICE_RESPONSE_ERROR);
  }
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
    let retryTimer: number | null = null;
    async function restoreSession() {
      try {
        const response = await fetch(`${apiBaseUrl}/auth/me`, { credentials: 'include', headers: { Accept: 'application/json' }, signal: controller.signal });
        if (response.status === 401) { setNotice(null); setAuthState('signed-out'); return; }
        const restoredAccount = await readJson<Account>(response);
        setAccount(restoredAccount); setNotice(null); setAuthState('signed-in');
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setNotice({ tone: 'error', message: SERVICE_RECONNECT_MESSAGE });
        setAuthState('checking');
        retryTimer = window.setTimeout(() => void restoreSession(), SESSION_RETRY_MS);
      }
    }
    void restoreSession();
    return () => {
      controller.abort();
      if (retryTimer !== null) window.clearTimeout(retryTimer);
    };
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
    const initialView = candidate === 'mt5' && !canManageMt5 ? 'settings' : candidate;
    setActiveView(initialView);
    window.history.replaceState(historyStateWithView(initialView), '', urlForView(initialView));
    function handlePopState(event: PopStateEvent) {
      const candidateView = workspaceViewFromHistory(event.state?.superSignalsView) ?? requestedWorkspaceView() ?? 'overview';
      const view = candidateView === 'mt5' && !canManageMt5 ? 'settings' : candidateView;
      setActiveView(view); setMenuOpen(false); if (view === 'settings') void refreshSharedSources();
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
      const loginView = requestedView === 'mt5' && !authenticatedAccount.permissions.includes('mt5_accounts.approve') ? 'settings' : requestedView ?? 'overview';
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
    setActiveView(view); setMenuOpen(false); if (view === 'settings') void refreshSharedSources();
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch { /* test environments */ }
  }

  if (authState === 'checking') return <main className="app-shell"><section className="auth-card auth-card--loading" aria-live="polite"><img className="brand-logo" src="/super-signals-logo.png" alt="Smart Signals" /><p>{notice?.message ?? 'Checking secure session…'}</p></section></main>;

  if (authState === 'signed-in' && account) {
    const visibleRoleLabel = account.role === 'user' ? 'Member' : account.role_label;
    const displayName = account.display_name ?? visibleRoleLabel;
    if (account.role === 'trading_admin' && !setupCheckComplete) return <main className="app-shell"><section className="auth-card auth-card--loading" aria-live="polite"><img className="brand-logo" src="/super-signals-logo.png" alt="Smart Signals" /><p>Checking your one-time Telegram setup…</p></section></main>;

    return <main className="app-shell workspace-shell"><section className="dashboard-card dashboard-card--workspace" aria-label="Smart Signals workspace">
      <header className="workspace-topbar">
        <button className="workspace-brand workspace-brand-button" type="button" aria-label="Go home" onClick={() => navigate('overview')}><img className="brand-logo" src="/super-signals-logo.png" alt="Smart Signals" /><span className="workspace-brand-copy"><strong>Private trading account</strong><small>Signal follower · broker controlled</small></span></button>
        <div className="workspace-topbar-actions">{activeView !== 'overview' && <button className="topbar-home-button" type="button" onClick={() => navigate('overview')}><span aria-hidden="true">⌂</span><span>Home</span></button>}<button className="menu-trigger" type="button" aria-label="Open menu" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}><span className="menu-bars" aria-hidden="true"><span /><span /><span /></span></button></div>
      </header>
      {menuOpen && <button className="menu-backdrop" type="button" aria-label="Close menu" onClick={() => setMenuOpen(false)} />}
      <aside className={`workspace-drawer ${menuOpen ? 'workspace-drawer--open' : ''}`} aria-label="Workspace navigation" aria-hidden={!menuOpen}>
        <div className="drawer-header"><div><strong>Smart Signals</strong><span>{visibleRoleLabel}</span></div><button className="menu-close" type="button" aria-label="Close menu" onClick={() => setMenuOpen(false)}>×</button></div>
        <nav className="drawer-nav" aria-label="Main menu">
          <button type="button" aria-current={activeView === 'overview' ? 'page' : undefined} onClick={() => navigate('overview')}><span className="drawer-nav-icon" aria-hidden="true">⌂</span><span className="drawer-nav-label"><strong>Home</strong><small>Balance, positions and trading activity</small></span></button>
          <button type="button" aria-current={activeView === 'settings' ? 'page' : undefined} onClick={() => navigate('settings')}><span className="drawer-nav-icon" aria-hidden="true">⚙</span><span className="drawer-nav-label"><strong>Settings</strong><small>Risk, MT5, sources and security</small></span></button>
        </nav>
        <div className="drawer-footer"><div className="drawer-account"><strong>{displayName}</strong><small>{account.email}</small></div><button className="button button--quiet" type="button" onClick={handleLogout} disabled={busy}>Log out</button></div>
      </aside>
      <div className="workspace-content">
        {activeView !== 'overview' && activeView !== 'settings' && activeView !== 'setup' && <button className="workspace-back-button" type="button" onClick={() => navigate('settings')}><span aria-hidden="true">←</span> Back to Settings</button>}
        {activeView === 'setup' && account.role === 'trading_admin' && canManageTelegram && <TradingAdminOnboarding apiBaseUrl={apiBaseUrl} displayName={displayName} onComplete={() => { void refreshSharedSources(); navigate('overview'); }} />}

        {activeView === 'overview' && <MobileDashboard apiBaseUrl={apiBaseUrl} displayName={displayName} roleLabel={visibleRoleLabel} onOpenSettings={() => navigate('settings')} />}

        {activeView === 'settings' && <section className="settings-page" aria-labelledby="settings-page-title">
          <div className="workspace-page-header"><div><p className="eyebrow">Account controls</p><h1 id="settings-page-title">Settings</h1><p className="intro">The daily dashboard stays focused on trading. Connections, risk and administration live here.</p></div><span className="workspace-role-pill">{visibleRoleLabel}</span></div>
          {account.role === 'user' && <div className="settings-user-stack"><TradingActivationPanel /><UserMt5ConnectionPanel apiBaseUrl={apiBaseUrl} /></div>}
          <div className="settings-grid" aria-label="Settings areas">
            <PushNotificationsDay34 apiBaseUrl={apiBaseUrl} />
            {account.role === 'trading_admin' && canManageTelegram && <article className="settings-card"><span className="status-label">First-time setup</span><h2>Telegram onboarding</h2><p>Connect your private reader and select the groups you administer.</p><button className="button button--quiet" type="button" onClick={() => navigate('setup')}>Open Telegram setup</button></article>}
            {canManageTelegram && <article className="settings-card"><span className="status-label">Signal network</span><h2>Telegram &amp; sources</h2><p>{sharedSourcesLoaded ? `${sharedSources.length} shared source${sharedSources.length === 1 ? '' : 's'} currently catalogued.` : 'Checking shared sources…'} Private reader sessions remain isolated per administrator.</p><div className="settings-actions"><button className="button button--quiet" type="button" onClick={() => navigate('sources')}>Signal sources</button><button className="button button--quiet" type="button" onClick={() => navigate('telegram')}>Reader accounts</button></div></article>}
            {canManageMt5 && <article className="settings-card"><span className="status-label">Owner broker tools</span><h2>Vantage MT5 demo</h2><p>Open the owner-only demo connection and terminal read diagnostics used for controlled acceptance.</p><button className="button button--quiet" type="button" onClick={() => navigate('mt5')}>Open MT5 demo</button></article>}
            <article className="settings-card"><span className="status-label">Account</span><h2>Account &amp; security</h2><p>{account.role === 'user' ? 'View your sign-in details and a simple summary of your saved setup.' : 'Review your role, additional security state and approved permissions.'}</p><button className="button button--quiet" type="button" onClick={() => navigate('access')}>Account &amp; security</button></article>
          </div>
        </section>}

        {activeView === 'telegram' && canManageTelegram && <section className="workspace-panel-page" aria-labelledby="telegram-page-title"><div className="workspace-page-header"><div><p className="eyebrow">Private reader connections</p><h1 id="telegram-page-title">Telegram accounts</h1><p className="intro">Each connected reader remains private to the administrator who authorised it.</p></div><span className="workspace-role-pill">Private sessions</span></div><TelegramConnectionPanel apiBaseUrl={apiBaseUrl} /></section>}
        {activeView === 'sources' && canManageTelegram && <section className="workspace-panel-page" aria-labelledby="sources-page-title"><div className="workspace-page-header"><div><p className="eyebrow">Shared Smart Signals data</p><h1 id="sources-page-title">Signal sources</h1><p className="intro">Add shared Telegram groups or channels and control each source as Testing, Live or Paused. LIVE is a source state only and does not enable trade execution.</p></div><span className="workspace-role-pill">STATE CONTROL</span></div><TelegramSourceSelector apiBaseUrl={apiBaseUrl} canViewOwnerAlerts={canViewOwnerAlerts} /></section>}
        {activeView === 'mt5' && canManageMt5 && <section className="workspace-panel-page" aria-labelledby="mt5-page-title"><div className="workspace-page-header"><div><p className="eyebrow">Owner broker connection</p><h1 id="mt5-page-title">MT5 demo</h1><p className="intro">Vantage demo connection and the controlled account-state / XAUUSD read gate.</p></div><span className="workspace-role-pill">OWNER DEMO</span></div><Mt5DemoConnectionPanel apiBaseUrl={apiBaseUrl} /></section>}
        {activeView === 'access' && account.role === 'user' && <section className="access-page" aria-labelledby="access-page-title"><div className="workspace-page-header"><div><p className="eyebrow">Your account</p><h1 id="access-page-title">Account &amp; security</h1><p className="intro">Everything here is a simple summary. There is nothing you need to configure on this page.</p></div><span className="workspace-role-pill">Member</span></div><div className="status-grid"><article className="status-card status-card--healthy"><span className="status-label">Account email</span><strong>{account.email}</strong><small>This is the email you use to sign in.</small></article><article className="status-card status-card--healthy"><span className="status-label">Account status</span><strong>Active</strong><small>Your Smart Signals membership is ready to use.</small></article><article className="status-card"><span className="status-label">Security</span><strong>{account.security.passkey === 'enabled' ? 'Passkey enabled' : 'Password protected'}</strong><small>{account.security.passkey === 'enabled' ? 'Your passkey is active on this account.' : 'No action needed. Extra sign-in options will be added later.'}</small></article></div><article className="settings-card"><span className="status-label">Saved setup</span><h2>Your choices are saved</h2><p>Your trading and notification choices are stored on your account. If you ever want to change them, go back to Settings.</p><button className="button button--quiet" type="button" onClick={() => navigate('settings')}>Back to Settings</button></article></section>}
        {activeView === 'access' && account.role !== 'user' && <section className="access-page" aria-labelledby="access-page-title"><div className="workspace-page-header"><div><p className="eyebrow">Account controls</p><h1 id="access-page-title">Access &amp; security</h1><p className="intro">Your role, security state and approved workspace permissions.</p></div><span className="workspace-role-pill">{visibleRoleLabel}</span></div><div className="status-grid"><article className="status-card status-card--healthy"><span className="status-label">Session</span><strong>Persistently signed in</strong><small>{account.email}</small></article><article className="status-card"><span className="status-label">Role</span><strong>{visibleRoleLabel}</strong><small>{account.permissions.length} approved permissions</small></article><article className="status-card"><span className="status-label">Additional security</span><strong>{account.security.two_factor === 'enabled' ? '2FA enabled' : 'Setup required'}</strong><small>{account.security.passkey === 'enabled' ? 'Passkey enabled' : 'Passkey setup available soon'}</small></article></div><div className="access-grid" aria-label="Approved workspace areas">{account.sections.map((section) => <article className={`access-section access-section--${section.key}`} key={section.key}><span className="status-label">{section.label}</span><p>{section.description}</p><ul className="access-action-list">{section.actions.map((action) => <li key={action.permission}><strong>{action.label}</strong><small>{action.description}</small></li>)}</ul></article>)}</div><div className="foundation-note"><span className="pulse" aria-hidden="true" />Role restrictions are enforced by the API and every denial is audited.</div></section>}
      </div>
    </section></main>;
  }

  return <main className="app-shell"><section className="auth-card" aria-labelledby="login-title"><img className="brand-logo" src="/super-signals-logo.png" alt="Smart Signals" /><p className="eyebrow">Private account access</p><h1 id="login-title">{showRecovery ? 'Recover access' : 'Sign in securely'}</h1><p className="intro">{showRecovery ? 'Enter the account email. The response will never reveal whether an account exists.' : 'Sign in once on this device. Smart Signals keeps the session active while you use the app.'}</p>{notice && <div className={`notice notice--${notice.tone}`} role="status">{notice.message}</div>}{showRecovery ? <form className="auth-form" onSubmit={handleRecovery}><label>Account email<input name="email" type="email" autoComplete="email" required /></label><button className="button" type="submit" disabled={busy}>{busy ? 'Submitting…' : 'Send recovery instructions'}</button><button className="text-button" type="button" onClick={() => { setShowRecovery(false); setNotice(null); }}>Back to sign in</button></form> : <form className="auth-form" onSubmit={handleLogin}><label>Email<input name="email" type="email" autoComplete="username" required /></label><label>Password<input name="password" type="password" autoComplete="current-password" required /></label><button className="button" type="submit" disabled={busy}>{busy ? 'Checking…' : 'Sign in'}</button><button className="text-button" type="button" onClick={() => { setShowRecovery(true); setNotice(null); }}>I cannot access my account</button></form>}</section></main>;
}
