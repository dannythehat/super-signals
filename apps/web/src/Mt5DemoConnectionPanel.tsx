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

export function Mt5DemoConnectionPanel({ apiBaseUrl }: { apiBaseUrl: string }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  async function load() {
    const response = await fetch(`${apiBaseUrl}/owner/mt5/demo/status`, { credentials: 'include', headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Unable to read MT5 connection status.');
    setConnection((await response.json()) as Connection);
  }

  useEffect(() => { void load().catch(() => setMessage('Unable to read MT5 connection status.')); }, []);

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setMessage('');
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch(`${apiBaseUrl}/owner/mt5/demo/connect`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ login: form.get('login'), password: form.get('password'), server: form.get('server') }),
      });
      const body = await response.json() as Connection | { detail?: { message?: string } };
      if (!response.ok) throw new Error('detail' in body ? body.detail?.message ?? 'Connection failed.' : 'Connection failed.');
      setConnection(body as Connection);
      event.currentTarget.reset();
      setMessage((body as Connection).status === 'connected' ? 'Vantage MT5 demo connected.' : 'Connection submitted. The broker bridge is still connecting.');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Connection failed.'); }
    finally { setBusy(false); }
  }

  async function refresh() {
    setBusy(true); setMessage('');
    try {
      const response = await fetch(`${apiBaseUrl}/owner/mt5/demo/refresh`, { method: 'POST', credentials: 'include', headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error('Status refresh failed.');
      setConnection((await response.json()) as Connection);
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Status refresh failed.'); }
    finally { setBusy(false); }
  }

  return <section aria-labelledby="mt5-demo-title">
    <div className="overview-hero">
      <p className="eyebrow">Day 22 · owner testing only</p>
      <h1 id="mt5-demo-title">Vantage MT5 demo</h1>
      <p className="intro">Connect the Vantage demo account. Super Signals handles the broker API connection behind the scenes. This screen cannot place trades.</p>
    </div>
    <div className="overview-grid">
      <article className="overview-card"><span className="status-label">Connection</span><strong>{connection?.status ?? 'Checking…'}</strong><small>{connection?.remote_state ?? 'Not configured'} · {connection?.remote_connection_status ?? 'No remote state'}</small></article>
      <article className="overview-card"><span className="status-label">Account number</span><strong>{connection?.login_masked ?? 'Not connected'}</strong><small>{connection?.server ?? 'Vantage demo server not set'}</small></article>
      <article className="overview-card"><span className="status-label">Trading</span><strong>Disabled</strong><small>Day 22 has no order-placement capability.</small></article>
    </div>
    {!connection?.configured && <form className="auth-form" onSubmit={connect} autoComplete="off">
      <label>Vantage MT5 account number<input name="login" inputMode="numeric" required autoComplete="off" /><small>Vantage calls this “MT5 Login” in the account email. It is the same number.</small></label>
      <label>Vantage MT5 trading password<input name="password" type="password" required autoComplete="new-password" /><small>Use the MT5 password from the Vantage account email, not your Vantage website password.</small></label>
      <label>Vantage MT5 server<input name="server" required autoComplete="off" placeholder="Exact server from your Vantage email" /><small>Copy the server name exactly as Vantage shows it.</small></label>
      <button className="button" type="submit" disabled={busy}>{busy ? 'Connecting…' : 'Connect demo account'}</button>
    </form>}
    {connection?.configured && <div className="overview-actions"><button className="button button--quiet" type="button" onClick={refresh} disabled={busy}>{busy ? 'Checking…' : 'Refresh connection'}</button></div>}
    {message && <p role="status">{message}</p>}
  </section>;
}
