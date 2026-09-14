import { FormEvent, useState } from 'react';

import './admin-member-onboarding.css';

type Props = {
  apiBaseUrl: string;
  onCompleted: () => Promise<void> | void;
};

type ConnectionAccepted = {
  user_id: string;
  attempt_id: string;
  status: 'connecting';
  stage: string;
  mt5_login_masked: string;
  mt5_server: string;
};

type ConnectionStatus = {
  user_id: string;
  attempt_id: string | null;
  status: string;
  stage: string;
  error_code: string | null;
  mt5_status: string | null;
  remote_state: string | null;
  remote_connection_status: string | null;
  trading_status: string | null;
  risk_percent: number | null;
  updated_at: string | null;
};

async function parseResponse<T>(response: Response): Promise<T> {
  let body: unknown = null;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body
      ? (body as { detail: unknown }).detail
      : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : typeof detail === 'string'
        ? detail
        : 'The complimentary member could not be onboarded safely.';
    throw new Error(message);
  }
  if (body === null) throw new Error('Smart Signals returned an invalid MT5 response.');
  return body as T;
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function stageLabel(stage: string): string {
  switch (stage) {
    case 'queued': return 'Queued securely';
    case 'server_preflight': return 'Checking the exact Vantage server';
    case 'stale_terminal_cleanup': return 'Cleaning previous MT5 connection state';
    case 'provisioning': return 'Creating the secure MT5 terminal';
    case 'broker_connect': return 'Connecting directly to Vantage';
    case 'redeploy_verification': return 'Verifying the broker session';
    case 'connected':
    case 'connected_existing': return 'Connected';
    case 'failed': return 'Connection failed';
    case 'interrupted': return 'Connection interrupted';
    default: return 'Connecting';
  }
}

function failureMessage(status: ConnectionStatus): string {
  const code = status.error_code || 'mt5_connection_failed';
  return `MT5 connection failed at ${stageLabel(status.stage)} (${code}). Trading remains stopped.`;
}

export function AdminMemberOnboarding({ apiBaseUrl, onCompleted }: Props) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<ConnectionAccepted | null>(null);
  const [connection, setConnection] = useState<ConnectionStatus | null>(null);

  async function refreshMembers(): Promise<void> {
    try { await onCompleted(); } catch { /* The connection status remains authoritative. */ }
  }

  async function pollConnection(started: ConnectionAccepted): Promise<void> {
    for (let attempt = 0; attempt < 150; attempt += 1) {
      try {
        const response = await fetch(`${apiBaseUrl}/admin/accounts/members/${started.user_id}/connection-v2`, {
          credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
        });
        const current = await parseResponse<ConnectionStatus>(response);
        if (current.attempt_id !== started.attempt_id) {
          await wait(2000);
          continue;
        }
        setConnection(current);
        if (current.status === 'connected') {
          setError(null);
          void refreshMembers();
          return;
        }
        if (current.status === 'failed' || current.status === 'interrupted') {
          setError(failureMessage(current));
          void refreshMembers();
          return;
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : 'Could not read the MT5 connection status.');
      }
      await wait(2000);
    }
    setError('The MT5 connection is still processing. You can close this panel and check the member card again.');
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true); setError(null); setAccepted(null); setConnection(null);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/accounts/members/onboard-live-v2`, {
        method: 'POST',
        credentials: 'include',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          email: String(data.get('email') || '').trim(),
          display_name: String(data.get('display_name') || '').trim(),
          mt5_login: String(data.get('mt5_login') || '').trim(),
          mt5_password: String(data.get('mt5_password') || ''),
          mt5_server: String(data.get('mt5_server') || '').trim(),
          complimentary_access: true,
        }),
      });
      const started = await parseResponse<ConnectionAccepted>(response);
      setAccepted(started);
      setConnection({
        user_id: started.user_id,
        attempt_id: started.attempt_id,
        status: 'connecting',
        stage: started.stage,
        error_code: null,
        mt5_status: 'connecting',
        remote_state: null,
        remote_connection_status: null,
        trading_status: 'stopped',
        risk_percent: 1,
        updated_at: null,
      });
      const password = form.elements.namedItem('mt5_password');
      if (password instanceof HTMLInputElement) password.value = '';
      setBusy(false);
      void refreshMembers();
      void pollConnection(started);
      return;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The complimentary member could not be onboarded safely.');
    } finally {
      setBusy(false);
    }
  }

  const connecting = connection?.status === 'connecting';
  const connected = connection?.status === 'connected';

  return <section className="member-onboard">
    <button type="button" className="member-onboard-toggle" onClick={() => setOpen((value) => !value)}>
      <span><strong>+ Add complimentary member</strong><small>Create free access and start a secure Vantage MT5 connection without holding the app open.</small></span>
      <b>{open ? 'Close' : 'Open'}</b>
    </button>

    {open && <form className="member-onboard-form" onSubmit={(event) => void submit(event)} autoComplete="off">
      <div className="member-onboard-grid">
        <label><span>Name</span><input name="display_name" required maxLength={120} placeholder="Member name" /></label>
        <label><span>Email</span><input name="email" required type="email" maxLength={320} placeholder="name@example.com" /></label>
        <label><span>Vantage MT5 login</span><input name="mt5_login" required inputMode="numeric" maxLength={32} placeholder="MT5 account number" /></label>
        <label><span>Exact Vantage server</span><input name="mt5_server" required maxLength={160} placeholder="VantageMarkets-Live ..." /></label>
        <label className="member-onboard-password"><span>MT5 trading password</span><input name="mt5_password" required type="password" maxLength={256} autoComplete="new-password" placeholder="Trading password" /><small>Used only for this broker connection attempt. Smart Signals does not store this password.</small></label>
      </div>
      <div className="member-onboard-actions"><button type="submit" disabled={busy || connecting}>{busy ? 'Starting secure connection…' : connecting ? 'Connection running…' : 'Add complimentary member & connect MT5'}</button></div>
      {error && <div className="member-onboard-error" role="alert">{error}</div>}
      {accepted && connection && <div className={`member-onboard-result ${connected ? 'member-onboard-result--ready' : ''}`} role="status">
        <strong>{connected ? 'Complimentary member connected and ready ✓' : stageLabel(connection.stage)}</strong>
        <span>{accepted.mt5_login_masked} · {accepted.mt5_server} · {connection.status}</span>
        <small>{connected ? `Automation active at ${connection.risk_percent ?? 1}% risk.` : 'Trading stays stopped until the broker session is genuinely connected.'}</small>
      </div>}
    </form>}
  </section>;
}
