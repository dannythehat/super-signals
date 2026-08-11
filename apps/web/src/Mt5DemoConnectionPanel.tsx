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

function connectionHelp(connection: Connection | null): string | null {
  if (!connection?.last_error_code) return null;
  const messages: Record<string, string> = {
    metaapi_permission_denied: 'MetaAPI refused the connection. Check the MetaAPI balance/subscription, then use Refresh connection.',
    metaapi_e_auth: 'Vantage rejected the MT5 credentials. Re-enter the MT5 login, trading password and exact server name below.',
    broker_credential_decryption_failed: 'The permanent broker encryption configuration needs administrator recovery. Do not create another MetaAPI account.',
    metaapi_timeout: 'MetaAPI did not answer in time. Wait briefly and use Refresh connection; do not reconnect repeatedly.',
    metaapi_unreachable: 'MetaAPI is temporarily unreachable. Use Refresh connection later; your saved MT5 link has not been deleted.',
  };
  return messages[connection.last_error_code] ?? `Connection needs attention (${connection.last_error_code}).`;
}

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
      setMessage((body as Connection).status === 'connected' ? 'Vantage MT5 demo connected.' : 'Connection submitted. The broker connection is still starting.');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Connection failed.'); }
    finally { setBusy(false); }
  }

  async function refresh() {
    setBusy(true); setMessage('');
    try {
      const response = await fetch(`${apiBaseUrl}/owner/mt5/demo/refresh`, { method: 'POST', credentials: 'include', headers: { Accept: 'application/json' } });
      const body = await response.json() as Connection | { detail?: { message?: string } };
      if (!response.ok) throw new Error('detail' in body ? body.detail?.message ?? 'Status refresh failed.' : 'Status refresh failed.');
      setConnection(body as Connection);
      setMessage((body as Connection).status === 'connected' ? 'Connection confirmed.' : 'Connection status refreshed.');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Status refresh failed.'); }
    finally { setBusy(false); }
  }

  const showCredentialForm = !connection?.configured || connection.status === 'error' || connection.status === 'disconnected';
  const help = connectionHelp(connection);

  return <section aria-labelledby="mt5-demo-title">
    <div className="overview-hero">
      <p className="eyebrow">MT5 connection</p>
      <h1 id="mt5-demo-title">Vantage MT5 demo</h1>
      <p className="intro">Your MetaAPI connection is managed by Super Signals. You only need your Vantage MT5 login, trading password and exact server name when first connecting or when Vantage credentials genuinely need to be re-entered. The MT5 password is never stored.</p>
    </div>
    <div className="overview-grid">
      <article className="overview-card"><span className="status-label">Connection</span><strong>{connection?.status ?? 'Checking…'}</strong><small>{connection?.remote_state ?? 'Not configured'} · {connection?.remote_connection_status ?? 'No remote state'}</small></article>
      <article className="overview-card"><span className="status-label">Account</span><strong>{connection?.login_masked ?? 'Not connected'}</strong><small>{connection?.server ?? 'Vantage demo server not set'}</small></article>
      <article className="overview-card"><span className="status-label">Trading</span><strong>Disabled</strong><small>This connection screen cannot place orders.</small></article>
    </div>
    {help && <p role="status">{help}</p>}
    {connection?.configured && <div className="overview-actions"><button className="button button--quiet" type="button" onClick={refresh} disabled={busy}>{busy ? 'Checking…' : 'Refresh connection'}</button></div>}
    {showCredentialForm && <form className="auth-form" onSubmit={connect} autoComplete="off">
      {connection?.configured && <p><strong>Reconnect only if needed.</strong> Your saved MetaAPI link is retained. Re-enter these fields only when Vantage credentials or the server connection need repairing.</p>}
      <label>Vantage MT5 account number<input name="login" inputMode="numeric" required autoComplete="off" /><small>Vantage calls this “MT5 Login” in the account email.</small></label>
      <label>Vantage MT5 trading password<input name="password" type="password" required autoComplete="new-password" /><small>Use the MT5 trading password, not your Vantage website password. Super Signals does not store it.</small></label>
      <label>Vantage MT5 server<input name="server" required autoComplete="off" defaultValue={connection?.server ?? ''} placeholder="Exact server name from your Vantage email" /></label>
      <button className="button" type="submit" disabled={busy}>{busy ? 'Connecting…' : connection?.configured ? 'Reconnect account' : 'Connect demo account'}</button>
    </form>}
    {message && <p role="status">{message}</p>}
  </section>;
}
