import { FormEvent, useEffect, useState } from 'react';

type Connection = {
  configured: boolean;
  broker: string;
  platform: string;
  account_environment: string;
  login_masked: string | null;
  server: string | null;
  status: string;
  remote_state: string | null;
  remote_connection_status: string | null;
  last_error_code: string | null;
};

type LiveState = {
  login_masked: string;
  server: string;
  region: string;
  read_at: string;
  account: {
    currency: string;
    balance: number;
    equity: number;
    margin: number;
    free_margin: number;
    margin_level: number | null;
    leverage: number | null;
    trade_allowed: boolean;
  };
  price: {
    symbol: string;
    bid: number | null;
    ask: number | null;
    buy_price: number | null;
    sell_price: number | null;
    quote_time: string | null;
    quote_age_seconds: number | null;
    available: boolean;
    stale: boolean;
    execution_ready: boolean;
    block_reason: string | null;
  };
  positions: Array<{
    position_id: string;
    symbol: string;
    side: string;
    volume: number;
    open_price: number;
    current_price: number | null;
    profit: number | null;
  }>;
  execution_ready: boolean;
  execution_block_reason: string | null;
};

function connectionHelp(connection: Connection | null): string | null {
  if (!connection?.last_error_code) return null;
  const messages: Record<string, string> = {
    metaapi_permission_denied: 'The broker connection needs attention. Smart Signals cannot currently access the required broker functions.',
    metaapi_e_auth: 'Vantage rejected the MT5 credentials. Re-enter the MT5 login, trading password and exact server name.',
    broker_credential_decryption_failed: 'The saved broker connection needs administrator recovery.',
    metaapi_timeout: 'The broker connection did not answer in time. Try Refresh connection again shortly.',
    metaapi_unreachable: 'The broker connection is temporarily unavailable. Your saved MT5 link has not been deleted.',
  };
  return messages[connection.last_error_code] ?? `The broker connection needs attention (${connection.last_error_code}).`;
}

function amount(value: number, currency: string): string {
  return new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: currency || 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function connectionLabel(connection: Connection | null): string {
  if (!connection) return 'Checking…';
  if (connection.status === 'connected' && connection.remote_connection_status === 'CONNECTED') return 'Connected';
  if (!connection.configured) return 'Not connected';
  return 'Needs attention';
}

export function Mt5DemoConnectionPanel({ apiBaseUrl }: { apiBaseUrl: string }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [liveState, setLiveState] = useState<LiveState | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [loaded, setLoaded] = useState(false);

  async function load() {
    const response = await fetch(`${apiBaseUrl}/owner/mt5/demo/status`, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    if (!response.ok) throw new Error('Unable to read the MT5 connection status.');
    setConnection((await response.json()) as Connection);
  }

  useEffect(() => {
    void load()
      .catch(() => setMessage('Unable to read the MT5 connection status.'))
      .finally(() => setLoaded(true));
  }, []);

  async function refresh() {
    setBusy(true);
    setMessage('');
    try {
      const response = await fetch(`${apiBaseUrl}/owner/mt5/demo/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { Accept: 'application/json' },
      });
      const body = await response.json() as Connection | { detail?: { message?: string } };
      if (!response.ok) {
        throw new Error('detail' in body ? body.detail?.message ?? 'Connection refresh failed.' : 'Connection refresh failed.');
      }
      setConnection(body as Connection);
      setMessage((body as Connection).status === 'connected' ? 'Vantage demo connection confirmed.' : 'Connection status refreshed.');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Connection refresh failed.');
    } finally {
      setBusy(false);
    }
  }

  async function readLiveState() {
    setBusy(true);
    setMessage('');
    try {
      const response = await fetch(`${apiBaseUrl}/owner/mt5/demo/live-state`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      const body = await response.json() as LiveState | { detail?: { message?: string } };
      if (!response.ok) {
        throw new Error('detail' in body ? body.detail?.message ?? 'Unable to read the demo account.' : 'Unable to read the demo account.');
      }
      setLiveState(body as LiveState);
      setMessage((body as LiveState).execution_ready ? 'Demo account is online and receiving a fresh XAUUSD quote.' : `Demo account read completed. ${(body as LiveState).execution_block_reason ?? 'Price is not currently executable.'}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Unable to read the demo account.');
    } finally {
      setBusy(false);
    }
  }

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setMessage('');
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch(`${apiBaseUrl}/owner/mt5/demo/connect`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ login: form.get('login'), password: form.get('password'), server: form.get('server') }),
      });
      const body = await response.json() as Connection | { detail?: { message?: string } };
      if (!response.ok) {
        throw new Error('detail' in body ? body.detail?.message ?? 'Connection failed.' : 'Connection failed.');
      }
      setConnection(body as Connection);
      event.currentTarget.reset();
      setMessage((body as Connection).status === 'connected' ? 'Vantage demo connected.' : 'Connection submitted.');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Connection failed.');
    } finally {
      setBusy(false);
    }
  }

  const connected = connection?.status === 'connected' && connection.remote_connection_status === 'CONNECTED';
  // Only offer the credential form once the real connection state is known.
  // Rendering it while the status request is still in flight briefly tells an
  // already-connected owner to reconnect, which reads as a broken account.
  const showCredentialForm =
    loaded && (!connection?.configured || connection.status === 'error' || connection.status === 'disconnected');
  const help = connectionHelp(connection);

  return <section aria-labelledby="paper-trading-title">
    <div className="overview-hero">
      <p className="eyebrow">Paper trading</p>
      <h1 id="paper-trading-title">Vantage demo account</h1>
      <p className="intro">This is the Smart Signals paper-trading account. It uses virtual funds only. Your broker connection is managed securely in the background.</p>
    </div>

    <div className="overview-grid">
      <article className={`overview-card ${connected ? 'status-card--healthy' : ''}`}>
        <span className="status-label">Connection</span>
        <strong>{connectionLabel(connection)}</strong>
        <small>{connected ? 'Broker link is online' : 'Broker link is not ready'}</small>
      </article>
      <article className="overview-card">
        <span className="status-label">MT5 account</span>
        <strong>{connection?.login_masked ?? 'Not connected'}</strong>
        <small>{connection?.server ?? 'Vantage demo server not set'}</small>
      </article>
      <article className="overview-card status-card--healthy">
        <span className="status-label">Mode</span>
        <strong>Paper Trading</strong>
        <small>Demo funds only · no real-money account</small>
      </article>
    </div>

    {help && <div className="day32-inline-warning" role="status"><strong>{help}</strong></div>}

    {connected && <div className="overview-actions">
      <button className="button button--quiet" type="button" onClick={refresh} disabled={busy}>{busy ? 'Checking…' : 'Refresh connection'}</button>
      <button className="button" type="button" onClick={readLiveState} disabled={busy}>{busy ? 'Checking…' : 'Check demo account'}</button>
    </div>}

    {liveState && <div className="overview-section" aria-label="Demo account state">
      <div className="overview-grid">
        <article className="overview-card"><span className="status-label">Balance</span><strong>{amount(liveState.account.balance, liveState.account.currency)}</strong><small>Equity {amount(liveState.account.equity, liveState.account.currency)}</small></article>
        <article className="overview-card"><span className="status-label">Free margin</span><strong>{amount(liveState.account.free_margin, liveState.account.currency)}</strong><small>Used margin {amount(liveState.account.margin, liveState.account.currency)}</small></article>
        <article className="overview-card"><span className="status-label">Open positions</span><strong>{liveState.positions.length}</strong><small>{liveState.account.trade_allowed ? 'Broker trading available' : 'Broker trading unavailable'}</small></article>
      </div>
      <div className="overview-grid">
        <article className="overview-card"><span className="status-label">XAUUSD</span><strong>{liveState.price.bid ?? '—'} / {liveState.price.ask ?? '—'}</strong><small>Bid / Ask</small></article>
        <article className="overview-card"><span className="status-label">Market data</span><strong>{liveState.execution_ready ? 'Ready' : 'Waiting'}</strong><small>{liveState.price.quote_age_seconds == null ? 'No fresh quote' : `${liveState.price.quote_age_seconds.toFixed(1)}s old`}</small></article>
        <article className="overview-card"><span className="status-label">Environment</span><strong>Demo</strong><small>{liveState.region}</small></article>
      </div>
    </div>}

    {showCredentialForm && <form className="auth-form" onSubmit={connect} autoComplete="off">
      <p><strong>Connect the Vantage demo account</strong></p>
      <p>Only use this form if the saved demo connection genuinely needs to be connected or repaired.</p>
      <label>Vantage MT5 account number<input name="login" inputMode="numeric" required autoComplete="off" /></label>
      <label>Vantage MT5 trading password<input name="password" type="password" required autoComplete="new-password" /></label>
      <label>Vantage MT5 server<input name="server" required autoComplete="off" defaultValue={connection?.server ?? ''} placeholder="Exact Vantage server name" /></label>
      <button className="button" type="submit" disabled={busy}>{busy ? 'Connecting…' : 'Connect demo account'}</button>
    </form>}

    {message && <p role="status"><strong>{message}</strong></p>}
  </section>;
}
