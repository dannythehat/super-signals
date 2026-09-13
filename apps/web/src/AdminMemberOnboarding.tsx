import { FormEvent, useState } from 'react';

import './admin-member-onboarding.css';

type Props = {
  apiBaseUrl: string;
  onCompleted: () => Promise<void> | void;
};

type OnboardResult = {
  user_id: string;
  email: string;
  display_name: string;
  created: boolean;
  complimentary_access: boolean;
  mt5_login_masked: string | null;
  mt5_server: string | null;
  mt5_status: string;
  remote_connection_status: string | null;
  trading_status: string;
  risk_percent: number;
  ready: boolean;
  welcome_email_sent: boolean;
  welcome_email_reason: string | null;
};

async function parseResponse(response: Response): Promise<OnboardResult> {
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
  return body as OnboardResult;
}

export function AdminMemberOnboarding({ apiBaseUrl, onCompleted }: Props) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<OnboardResult | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true); setError(null); setResult(null);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/accounts/members/onboard-live`, {
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
        }),
      });
      const completed = await parseResponse(response);
      setResult(completed);
      const password = form.elements.namedItem('mt5_password');
      if (password instanceof HTMLInputElement) password.value = '';
      await onCompleted();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The complimentary member could not be onboarded safely.');
    } finally { setBusy(false); }
  }

  return <section className="member-onboard">
    <button type="button" className="member-onboard-toggle" onClick={() => setOpen((value) => !value)}>
      <span><strong>+ Add complimentary member</strong><small>Create free access, verify the live Vantage MT5 account and add it to Smart Signals in one step.</small></span>
      <b>{open ? 'Close' : 'Open'}</b>
    </button>

    {open && <form className="member-onboard-form" onSubmit={(event) => void submit(event)} autoComplete="off">
      <div className="member-onboard-grid">
        <label><span>Name</span><input name="display_name" required maxLength={120} placeholder="Member name" /></label>
        <label><span>Email</span><input name="email" required type="email" maxLength={320} placeholder="name@example.com" /></label>
        <label><span>Vantage MT5 login</span><input name="mt5_login" required inputMode="numeric" maxLength={32} placeholder="MT5 account number" /></label>
        <label><span>Exact Vantage server</span><input name="mt5_server" required maxLength={160} placeholder="VantageMarkets-Live ..." /></label>
        <label className="member-onboard-password"><span>MT5 trading password</span><input name="mt5_password" required type="password" maxLength={256} autoComplete="new-password" placeholder="Trading password" /><small>Used only to verify the broker connection. Smart Signals does not store this password.</small></label>
      </div>
      <div className="member-onboard-actions"><button type="submit" disabled={busy}>{busy ? 'Connecting & verifying…' : 'Add complimentary member & connect MT5'}</button></div>
      {error && <div className="member-onboard-error" role="alert">{error}</div>}
      {result && <div className={`member-onboard-result ${result.ready ? 'member-onboard-result--ready' : ''}`} role="status">
        <strong>{result.ready ? 'Complimentary member connected and ready ✓' : 'Complimentary account created — MT5 still verifying'}</strong>
        <span>{result.display_name} · {result.mt5_login_masked || 'MT5'} · {result.mt5_status} · {result.risk_percent}% risk</span>
        <small>{result.welcome_email_sent ? 'Branded Smart Signals welcome email sent.' : `Welcome email not sent yet${result.welcome_email_reason ? ` (${result.welcome_email_reason})` : ''}.`}</small>
      </div>}
    </form>}
  </section>;
}
