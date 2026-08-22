import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { App } from '../src/App';

const owner = {
  id: '9e5d24de-d6d5-4dad-b6bd-8ff43fbb13c6',
  email: 'owner@example.com',
  display_name: 'Danny',
  role: 'owner',
  role_label: 'Owner Admin',
  roles: ['owner'],
  permissions: ['users.manage', 'admins.manage', 'sources.manage', 'mt5_accounts.approve', 'account.connect'],
  sections: [
    {
      key: 'owner', label: 'Owner controls', description: 'Users, access, approvals and security.',
      actions: [{ permission: 'users.manage', label: 'Invited users', description: 'Invite, suspend or revoke users.' }],
    },
    {
      key: 'trading', label: 'Trading operations', description: 'Telegram sources and review activity.',
      actions: [{ permission: 'sources.manage', label: 'Signal sources', description: 'Add, pause, resume or remove sources.' }],
    },
    {
      key: 'user', label: 'My approved access', description: 'Approved account and automation.',
      actions: [{ permission: 'account.connect', label: 'MT5 account', description: 'Connect one approved account.' }],
    },
  ],
  security: { two_factor: 'setup_required', passkey: 'setup_available' },
};

const tradingAdmin = {
  ...owner,
  id: '4fa398b1-65e2-445d-b7ad-08c6d1785f89',
  email: 'trading@example.com',
  display_name: 'Trading Admin',
  role: 'trading_admin',
  role_label: 'Trading Admin',
  roles: ['trading_admin'],
  permissions: ['sources.manage'],
  sections: [owner.sections[1]],
};

const invitedUser = {
  ...owner,
  id: '2791cc0b-d501-4e51-8b96-72262f11df0b',
  email: 'user@example.com',
  display_name: 'Invited User',
  role: 'user',
  role_label: 'Invited User',
  roles: ['user'],
  permissions: ['account.connect'],
  sections: [owner.sections[2]],
};

const sharedSource = {
  source_id: '11111111-1111-4111-8111-111111111111',
  chat_id: -1001651583302,
  title: 'FXTradingVision | Forex & Crypto Signals 🚀',
  status: 'paused',
};

const dashboard = {
  connection: {
    configured: false,
    status: 'not_configured',
    account_environment: null,
    login_masked: null,
    server: null,
    error_code: null,
    read_at: null,
  },
  account: null,
  trading: {
    available: false,
    status: null,
    risk_percent: null,
    allow_double_lot: null,
    effective_double_lot_risk_percent: null,
  },
  open_profit: null,
  open_positions: [],
  latest_signal: null,
  recent_completed: [],
  performance: [
    { key: 'today', label: 'Today', amount: 0, known_position_count: 0, provisional_until_day33: false },
    { key: 'week', label: 'This week', amount: 0, known_position_count: 0, provisional_until_day33: false },
    { key: 'month', label: 'This month', amount: 0, known_position_count: 0, provisional_until_day33: false },
    { key: 'all', label: 'All time', amount: 0, known_position_count: 0, provisional_until_day33: false },
  ],
  performance_timezone: 'UTC',
  daily_profit: [],
  win_loss: { wins: 0, losses: 0, breakeven: 0, known_results: 0, win_rate_percent: null },
  activity: [],
  reconciled_external_positions: 0,
  canonical_performance_ready: true,
  performance_basis: 'canonical_user_trading_ledger',
  broker_trade_action_created: false,
};

const todaySummary = {
  timezone: 'UTC',
  session_started_at: '2026-08-22T00:00:00Z',
  trades: 0,
  wins: 0,
  losses: 0,
  breakeven: 0,
  open: 0,
  pending: 0,
  settling: 0,
  realised_pnl: 0,
  winning_pips: 0,
  net_pips: 0,
  broker_trade_action_created: false,
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
  } as Response;
}

function htmlResponse(status: number): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': 'text/html; charset=utf-8' }),
    json: async () => { throw new SyntaxError("Unexpected token '<'"); },
  } as unknown as Response;
}

type MockOptions = {
  session?: typeof owner | typeof tradingAdmin | typeof invitedUser | null;
  loginAccount?: typeof owner | typeof tradingAdmin | typeof invitedUser;
  sources?: typeof sharedSource[];
};

function appFetchMock(options: MockOptions = {}) {
  const { session = owner, loginAccount = owner, sources = [] } = options;
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/auth/me')) return session ? jsonResponse(200, session) : jsonResponse(401, {});
    if (url.endsWith('/auth/login')) return jsonResponse(200, loginAccount);
    if (url.endsWith('/auth/logout')) return jsonResponse(204, {});
    if (url.endsWith('/auth/recovery')) return jsonResponse(202, { message: 'If the account is eligible, recovery instructions will be sent.' });
    if (url.endsWith('/admin/telegram/sources/shared')) return jsonResponse(200, sources);
    if (url.endsWith('/admin/telegram/accounts')) return jsonResponse(200, [{ id: 'reader-1', status: 'connected' }]);
    if (url.includes('/admin/telegram/sources/accounts/reader-1/available')) return jsonResponse(200, [{ selected: true, managed_by_this_reader: true }]);
    if (url.includes('/account/mt5/dashboard/today?')) return jsonResponse(200, todaySummary);
    if (url.endsWith('/account/mt5/dashboard') || url.includes('/account/mt5/dashboard?')) return jsonResponse(200, dashboard);
    if (url.endsWith('/account/mt5/dashboard/performance/sync')) return jsonResponse(200, { synced: true });
    if (url.includes('/account/mt5/dashboard/performance/timeline')) return jsonResponse(200, { trades: [], open_count: 0, pending_count: 0, provider_identity_visible: false, broker_trade_action_created: false });
    if (url.endsWith('/account/mt5/manual-actions')) return jsonResponse(200, { stop_loss_changes: 0, take_profit_changes: 0, manual_closes: 0, actions: [], broker_trade_action_created: false });
    if (init?.method === 'POST') return jsonResponse(200, {});
    return jsonResponse(404, { detail: { message: 'Not configured in this focused UI test.' } });
  });
}

describe('App', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState({}, '', window.location.pathname);
  });

  it('shows account login when no valid session exists', async () => {
    vi.stubGlobal('fetch', appFetchMock({ session: null }));
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Sign in securely' })).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('keeps the session in reconnecting state during a temporary service restart', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private network details')));
    render(<App />);
    expect(await screen.findByText('Reconnecting to the secure service…')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Sign in securely' })).not.toBeInTheDocument();
    expect(screen.queryByText('private network details')).not.toBeInTheDocument();
  });

  it('never exposes an HTML parser error if login meets a platform restart page', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/auth/me')) return jsonResponse(401, {});
      if (url.endsWith('/auth/login')) return htmlResponse(503);
      return jsonResponse(404, { detail: { message: 'Not configured.' } });
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.change(await screen.findByLabelText('Email'), { target: { value: 'owner@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct horse battery staple' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('The secure service is reconnecting. Please try again in a moment.')).toBeInTheDocument();
    expect(screen.queryByText(/Unexpected token/)).not.toBeInTheDocument();
    expect(screen.queryByText(/DOCTYPE/)).not.toBeInTheDocument();
  });

  it('signs the owner in to the current broker-backed Home', async () => {
    const fetchMock = appFetchMock({ session: null, loginAccount: owner });
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.change(await screen.findByLabelText('Email'), { target: { value: 'owner@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct horse battery staple' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('heading', { name: 'Hi, Danny' })).toBeInTheDocument();
    expect(screen.getByText('MT5 setup needed')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/login', expect.objectContaining({ method: 'POST', credentials: 'include' }));
  });

  it('opens the shared source workspace from current Settings', async () => {
    const fetchMock = appFetchMock({ session: owner, sources: [sharedSource] });
    vi.stubGlobal('fetch', fetchMock);
    window.scrollTo = vi.fn();
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Hi, Danny' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open Settings' }));
    expect(await screen.findByRole('heading', { name: 'Settings' })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/admin/telegram/sources/shared', expect.anything()));
    fireEvent.click(screen.getByRole('button', { name: 'Signal sources' }));
    expect(await screen.findByRole('heading', { name: 'Signal sources' })).toBeInTheDocument();
  });

  it('keeps owner operational tools inside Settings rather than the daily Home', async () => {
    vi.stubGlobal('fetch', appFetchMock({ session: owner }));
    window.scrollTo = vi.fn();
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Hi, Danny' })).toBeInTheDocument();
    expect(screen.queryByText('Telegram & sources')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    fireEvent.click(screen.getByRole('button', { name: /^Settings/ }));
    expect(await screen.findByText('Telegram & sources')).toBeInTheDocument();
    expect(screen.getByText('Vantage MT5 demo')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Account & security' })).toBeInTheDocument();
  });

  it('shows Telegram source tools to a configured Trading Admin without Owner broker tools', async () => {
    vi.stubGlobal('fetch', appFetchMock({ session: tradingAdmin }));
    window.scrollTo = vi.fn();
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Hi, Trading Admin' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open Settings' }));
    expect(await screen.findByText('Telegram & sources')).toBeInTheDocument();
    expect(screen.queryByText('Vantage MT5 demo')).not.toBeInTheDocument();
  });

  it('keeps Telegram and Owner broker management out of an invited member Settings page', async () => {
    vi.stubGlobal('fetch', appFetchMock({ session: invitedUser }));
    window.scrollTo = vi.fn();
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Hi, Invited User' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open Settings' }));
    expect(await screen.findByRole('heading', { name: 'Settings' })).toBeInTheDocument();
    expect(screen.queryByText('Telegram & sources')).not.toBeInTheDocument();
    expect(screen.queryByText('Vantage MT5 demo')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Account & security' })).toBeInTheDocument();
  });

  it('revokes the visible session and returns to login on logout', async () => {
    const fetchMock = appFetchMock({ session: owner });
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Open menu' }));
    fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    expect(await screen.findByRole('heading', { name: 'Sign in securely' })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/auth/logout', expect.objectContaining({ method: 'POST', credentials: 'include' })));
  });

  it('uses the same safe recovery response for every email', async () => {
    vi.stubGlobal('fetch', appFetchMock({ session: null }));
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'I cannot access my account' }));
    expect(screen.getByRole('heading', { name: 'Recover access' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Account email'), { target: { value: 'unknown@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send recovery instructions' }));
    expect(await screen.findByText('If the account is eligible, recovery instructions will be sent.')).toBeInTheDocument();
  });
});