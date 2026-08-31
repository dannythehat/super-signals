import { FormEvent, MouseEvent, useCallback, useEffect, useMemo, useState } from 'react';

const VANTAGE_AFFILIATE_URL = import.meta.env.VITE_VANTAGE_AFFILIATE_URL
  || 'https://vigco.co/la-com-inv/rVbJG9xZ';

type Mt5Profile = {
  id: string;
  account_environment: 'demo' | 'live';
  label: string;
  login_masked: string;
  server: string;
  status: string;
  remote_state: string | null;
  remote_connection_status: string | null;
  last_error_code: string | null;
  last_checked_at: string | null;
  last_connected_at: string | null;
  active: boolean;
};

type Mt5ProfilesResponse = {
  active_environment: string | null;
  switch_blocked: boolean;
  switch_block_reason: string | null;
  open_or_pending_position_count: number;
  profiles: Mt5Profile[];
  real_execution_enabled: boolean;
};

type Props = { apiBaseUrl: string };

async function readJson<T>(response: Response): Promise<T> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    if (!response.ok) throw new Error('Smart Signals could not complete the MT5 request. Please try again.');
  }
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body
      ? (body as { detail: unknown }).detail
      : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : 'The Vantage MT5 account could not be connected.';
    throw new Error(message);
  }
  return body as T;
}

function openVantageExternally(event: MouseEvent<HTMLAnchorElement>) {
  if (!/Android/i.test(window.navigator.userAgent)) return;
  event.preventDefault();
  const target = new URL(VANTAGE_AFFILIATE_URL);
  const fallback = encodeURIComponent(VANTAGE_AFFILIATE_URL);
  const scheme = target.protocol.replace(':', '');
  const intentUrl = `intent://${target.host}${target.pathname}${target.search}#Intent;scheme=${scheme};action=android.intent.action.VIEW;S.browser_fallback_url=${fallback};end`;
  window.location.href = intentUrl;
}

function isConnected(profile: Mt5Profile | undefined): boolean {
  return Boolean(
    profile
    && profile.status === 'connected'
    && profile.remote_state === 'DEPLOYED'
    && profile.remote_connection_status === 'CONNECTED',
  );
}

export function UserMt5ConnectionPanel({ apiBaseUrl }: Props) {
  const [data, setData] = useState<Mt5ProfilesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/profiles`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      setData(await readJson<Mt5ProfilesResponse>(response));
      setNotice(null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'MT5 status is temporarily unavailable.');
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => { void refresh(); }, [refresh]);

  const activeProfile = useMemo(
    () => data?.profiles.find((profile) => profile.active),
    [data],
  );
  const connected = isConnected(activeProfile);
  const shouldShowForm = showForm || (!loading && !connected);

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/profiles/connect`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          login: form.get('login'),
          password: form.get('password'),
          server: form.get('server'),
          make_active: true,
        }),
      });
      const next = await readJson<Mt5ProfilesResponse>(response);
      setData(next);
      const nextActive = next.profiles.find((profile) => profile.active);
      if (!isConnected(nextActive)) {
        throw new Error('MetaAPI created the MT5 terminal, but the Vantage broker session is not connected yet. Please retry once.');
      }
      event.currentTarget.reset();
      setShowForm(false);
      setNotice('Vantage MT5 connected successfully.');
      window.dispatchEvent(new Event('super-signals-mt5-connected'));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'The Vantage MT5 account could not be connected.');
    } finally {
      setBusy(false);
    }
  }

  return <section className="settings-panel settings-panel--mt5" aria-labelledby="user-mt5-settings-title">
    <div className="settings-panel-heading">
      <div><span className="status-label">Vantage MT5</span><h2 id="user-mt5-settings-title">Trading account</h2></div>
      {!loading && <span className={`settings-state settings-state--${connected ? 'good' : 'attention'}`}>{connected ? 'CONNECTED' : 'SETUP NEEDED'}</span>}
    </div>

    {loading ? <p className="muted-copy">Checking your trading account…</p> : <>
      {connected && activeProfile && <>
        <div className="settings-account-summary">
          <div><span>Account</span><strong>{activeProfile.login_masked}</strong></div>
          <div><span>Server</span><strong>{activeProfile.server}</strong></div>
          <div><span>Status</span><strong>Connected</strong></div>
        </div>
        <p className="settings-help">Your Vantage MT5 account is connected to Smart Signals. No further broker setup is required.</p>
        {!showForm && <div className="settings-actions">
          <button className="button button--quiet" type="button" onClick={() => setShowForm(true)}>Connect another account</button>
        </div>}
      </>}

      {shouldShowForm && <>
        {!connected && <div className="settings-vantage-start">
          <strong>Need a Vantage account?</strong>
          <p>Create your Vantage account using the Smart Signals referral link, then copy the MT5 details Vantage gives you.</p>
          <a className="button button--quiet" href={VANTAGE_AFFILIATE_URL} target="_blank" rel="noopener noreferrer" onClick={openVantageExternally}>Create a Vantage account ↗</a>
        </div>}
        <p className="settings-help">Copy the MT5 account number, trading password and exact server from Vantage. Smart Signals uses the password only to establish the broker connection and never stores it.</p>
        <form className="settings-mt5-form" onSubmit={connect}>
          <label>MT5 account number<input name="login" inputMode="numeric" autoComplete="off" required /></label>
          <label>MT5 trading password<input name="password" type="password" autoComplete="off" required /></label>
          <label>Exact Vantage server<input name="server" autoComplete="off" placeholder="e.g. VantageMarkets-Demo" required /></label>
          <p>Demo and Real Vantage MT5 accounts use the same secure connection flow. The exact server name matters.</p>
          <div className="settings-actions">
            <button className="button" type="submit" disabled={busy}>{busy ? 'Connecting…' : 'Connect MT5'}</button>
            {connected && <button className="button button--quiet" type="button" disabled={busy} onClick={() => setShowForm(false)}>Cancel</button>}
          </div>
        </form>
      </>}
    </>}
    {notice && <p className="settings-notice" role="status">{notice}</p>}
  </section>;
}
