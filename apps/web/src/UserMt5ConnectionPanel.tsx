import { FormEvent, useCallback, useEffect, useState } from 'react';

type MemberMt5OnboardingStatus = {
  request_status: string;
  request_login_masked: string | null;
  request_server: string | null;
  request_updated_at: string | null;
  approved: boolean;
  approval_status: string;
  approved_login_masked: string | null;
  approved_server: string | null;
  configured: boolean;
  connection_status: string;
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
  const [status, setStatus] = useState<MemberMt5OnboardingStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [editRequest, setEditRequest] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/onboarding`, {
        credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
      });
      setStatus(await readJson<MemberMt5OnboardingStatus>(response));
      setNotice(null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'MT5 status is unavailable.');
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => { void refreshStatus(); }, [refreshStatus]);

  async function submitRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setNotice(null);
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/onboarding/request`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ login: form.get('login'), server: form.get('server') }),
      });
      setStatus(await readJson<MemberMt5OnboardingStatus>(response));
      setEditRequest(false);
      setNotice('MT5 details sent for approval.');
      event.currentTarget.reset();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Your MT5 details could not be submitted.');
    } finally { setBusy(false); }
  }

  async function connectApproved(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setNotice(null);
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/onboarding/connect`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ password: form.get('password') }),
      });
      const next = await readJson<MemberMt5OnboardingStatus>(response);
      setStatus(next);
      setNotice(next.connection_status === 'connected' ? 'Vantage MT5 connected.' : 'MT5 setup is processing.');
      event.currentTarget.reset();
      window.dispatchEvent(new Event('super-signals-mt5-connected'));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'The Vantage MT5 account could not be connected.');
    } finally { setBusy(false); }
  }

  const connected = status?.connection_status === 'connected';
  const waiting = status?.request_status === 'requested' && !status.approved;
  const canSubmit = !status?.approved && (!waiting || editRequest);

  return <section className="settings-panel settings-panel--mt5" aria-labelledby="user-mt5-settings-title">
    <div className="settings-panel-heading">
      <div><span className="status-label">Vantage MT5</span><h2 id="user-mt5-settings-title">Trading account</h2></div>
      {status && <span className={`settings-state settings-state--${connected ? 'good' : 'attention'}`}>{connected ? 'CONNECTED' : status.approved ? 'APPROVED' : waiting ? 'WAITING FOR APPROVAL' : 'SETUP NEEDED'}</span>}
    </div>

    {loading ? <p className="muted-copy">Checking your trading account…</p> : status ? <>
      {connected && <>
        <div className="settings-account-summary">
          <div><span>Account</span><strong>{status.approved_login_masked ?? 'Connected'}</strong></div>
          <div><span>Server</span><strong>{status.approved_server ?? 'Vantage MT5'}</strong></div>
          <div><span>Status</span><strong>Connected</strong></div>
        </div>
        <p className="settings-help">Your approved Vantage MT5 account is connected to Super Signals.</p>
      </>}

      {!connected && !status.approved && !waiting && <>
        <p className="settings-help">Enter the Vantage MT5 account number and exact server you received from Vantage. Your trading password is not needed yet.</p>
        <form className="settings-mt5-form" onSubmit={submitRequest}>
          <label>MT5 account number<input name="login" inputMode="numeric" autoComplete="off" required /></label>
          <label>Exact Vantage server<input name="server" autoComplete="off" placeholder="e.g. VantageInternational-Live" required /></label>
          <p>We send these two details to the Owner for verification. Never send your MT5 trading password by email or Telegram.</p>
          <button className="button" type="submit" disabled={busy}>{busy ? 'Sending…' : 'Send for approval'}</button>
        </form>
      </>}

      {!connected && waiting && !editRequest && <>
        <div className="settings-account-summary">
          <div><span>Account</span><strong>{status.request_login_masked ?? 'Submitted'}</strong></div>
          <div><span>Server</span><strong>{status.request_server ?? 'Submitted'}</strong></div>
          <div><span>Approval</span><strong>Waiting</strong></div>
        </div>
        <p className="settings-help">Your MT5 details are waiting for Owner approval. Nothing can trade on your account while approval is pending.</p>
        <div className="settings-actions"><button className="button button--quiet" type="button" onClick={() => setEditRequest(true)}>Correct details</button><button className="button button--quiet" type="button" disabled={busy} onClick={() => void refreshStatus()}>Check approval</button></div>
      </>}

      {!connected && waiting && editRequest && <form className="settings-mt5-form" onSubmit={submitRequest}>
        <label>MT5 account number<input name="login" inputMode="numeric" autoComplete="off" required /></label>
        <label>Exact Vantage server<input name="server" defaultValue={status.request_server ?? ''} autoComplete="off" required /></label>
        <p>Submitting again replaces the pending details. Your MT5 password is still not requested.</p>
        <div className="settings-actions"><button className="button" type="submit" disabled={busy}>{busy ? 'Sending…' : 'Update request'}</button><button className="button button--quiet" type="button" disabled={busy} onClick={() => setEditRequest(false)}>Cancel</button></div>
      </form>}

      {!connected && status.approved && <>
        <div className="settings-account-summary">
          <div><span>Account</span><strong>{status.approved_login_masked ?? 'Approved'}</strong></div>
          <div><span>Server</span><strong>{status.approved_server ?? 'Vantage MT5'}</strong></div>
          <div><span>Approval</span><strong>Approved</strong></div>
        </div>
        <p className="settings-help">Your account is approved. Enter only your MT5 trading password below to establish the secure connection.</p>
        <form className="settings-mt5-form" onSubmit={connectApproved}>
          <label>MT5 trading password<input name="password" type="password" autoComplete="off" required /></label>
          <p>Your password is used only for this secure broker connection request. It is not stored in PostgreSQL and is never shown to the Owner.</p>
          <button className="button" type="submit" disabled={busy}>{busy ? 'Connecting…' : 'Connect MT5'}</button>
        </form>
      </>}

      {canSubmit && waiting && editRequest ? null : null}
    </> : null}
    {notice && <p className="settings-notice" role="status">{notice}</p>}
  </section>;
}
