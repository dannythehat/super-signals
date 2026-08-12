import { FormEvent, useCallback, useEffect, useState } from 'react';

type UserMt5Status = {
  approved: boolean;
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
  last_checked_at: string | null;
  last_connected_at: string | null;
};

type Props = { apiBaseUrl: string };

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? (body as { detail: unknown }).detail : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail ? String((detail as { message: unknown }).message) : 'MT5 account action failed.';
    throw new Error(message);
  }
  return body;
}

export function UserMt5ConnectionPanel({ apiBaseUrl }: Props) {
  const [status, setStatus] = useState<UserMt5Status | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [showConnect, setShowConnect] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/status`, { credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store' });
      const next = await readJson<UserMt5Status>(response);
      setStatus(next);
      setShowConnect(next.approved && next.status !== 'connected');
      setNotice(null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'MT5 status is unavailable.');
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => { void refreshStatus(); }, [refreshStatus]);

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true); setNotice(null);
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/connect`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ login: form.get('login'), password: form.get('password'), server: form.get('server') }),
      });
      const next = await readJson<UserMt5Status>(response);
      setStatus(next); setShowConnect(false); setNotice('Approved Vantage MT5 account connected.');
      event.currentTarget.reset();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'The Vantage MT5 account could not be connected.');
    } finally { setBusy(false); }
  }

  async function refreshConnection() {
    setBusy(true); setNotice(null);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/refresh`, { method: 'POST', credentials: 'include', headers: { Accept: 'application/json' } });
      const next = await readJson<UserMt5Status>(response);
      setStatus(next); setShowConnect(next.approved && next.status !== 'connected');
      setNotice(next.status === 'connected' ? 'MT5 connection refreshed.' : 'MT5 still needs attention.');
    } catch (error) { setNotice(error instanceof Error ? error.message : 'MT5 refresh failed.'); }
    finally { setBusy(false); }
  }

  return <section className="settings-panel settings-panel--mt5" aria-labelledby="user-mt5-settings-title">
    <div className="settings-panel-heading"><div><span className="status-label">Vantage MT5</span><h2 id="user-mt5-settings-title">Trading account</h2></div>{status && <span className={`settings-state settings-state--${status.status === 'connected' ? 'good' : 'attention'}`}>{status.status === 'connected' ? 'CONNECTED' : status.approved ? 'SETUP NEEDED' : 'AWAITING APPROVAL'}</span>}</div>
    {loading ? <p className="muted-copy">Checking your approved MT5 account…</p> : status ? <>
      <div className="settings-account-summary"><div><span>Approval</span><strong>{status.approved ? 'Owner approved' : 'Not approved yet'}</strong></div><div><span>Account</span><strong>{status.login_masked ?? 'Not connected'}</strong></div><div><span>Server</span><strong>{status.server ?? '—'}</strong></div></div>
      {!status.approved && <p className="settings-help">The Owner must approve one Vantage live MT5 login and server before you can connect it here.</p>}
      {status.status === 'connected' && <div className="settings-actions"><button className="button button--quiet" type="button" disabled={busy} onClick={() => void refreshConnection()}>{busy ? 'Refreshing…' : 'Refresh connection'}</button><button className="text-button" type="button" onClick={() => setShowConnect((value) => !value)}>Reconnect credentials</button></div>}
      {status.approved && status.status !== 'connected' && !showConnect && <button className="button" type="button" onClick={() => setShowConnect(true)}>Connect approved account</button>}
      {showConnect && status.approved && <form className="settings-mt5-form" onSubmit={connect}><label>MT5 account number<input name="login" inputMode="numeric" autoComplete="off" required /></label><label>Exact Vantage server<input name="server" defaultValue={status.server ?? ''} autoComplete="off" required /></label><label>MT5 trading password<input name="password" type="password" autoComplete="off" required /></label><p>Your trading password is sent only to the secure connection service for this request and is not stored in PostgreSQL.</p><div className="settings-actions"><button className="button" type="submit" disabled={busy}>{busy ? 'Connecting…' : 'Connect MT5'}</button>{status.status === 'connected' && <button className="button button--quiet" type="button" onClick={() => setShowConnect(false)}>Cancel</button>}</div></form>}
    </> : null}
    {notice && <p className="settings-notice" role="status">{notice}</p>}
  </section>;
}
