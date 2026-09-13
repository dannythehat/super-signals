import { useCallback, useEffect, useMemo, useState } from 'react';

import { AdminMemberOnboarding } from './AdminMemberOnboarding';
import './admin-member-controls-day35.css';

type ManagedUser = {
  user_id: string;
  email: string;
  display_name: string | null;
  status: string;
  trading_status: string | null;
  risk_percent: number | null;
  allow_double_lot: boolean | null;
  mt5_status: string | null;
  mt5_login_masked: string | null;
  mt5_server: string | null;
  mapped_open_positions: number;
  mapped_pending_positions: number;
  active_sessions: number;
  push_devices_enabled: number;
};

type MemberAccessState = {
  user_id: string;
  email: string;
  display_name: string | null;
  status: string;
  active: boolean;
  plan_code: string | null;
  active_until: string | null;
};

type MemberAccessMutation = MemberAccessState & { message: string };

type RevokePreview = {
  user: ManagedUser;
  confirmation_text: string;
  confirmation_title: string;
  confirmation_message: string;
  mapped_positions_to_close: number;
  manual_or_unmapped_positions_touched: boolean;
  broker_trade_action_created: boolean;
};

type RevokeResult = {
  user_id: string;
  status: string;
  automation_status: string;
  broker_actions_sent: number;
  mapped_positions_closed: number;
  external_positions_reconciled: number;
  sessions_revoked: number;
  approvals_revoked: number;
  push_devices_disabled: number;
  manual_or_unmapped_positions_touched: boolean;
};

type ReconnectResult = {
  user_id: string;
  email: string;
  display_name: string;
  mt5_login_masked: string | null;
  mt5_server: string | null;
  mt5_status: string;
  remote_state: string | null;
  remote_connection_status: string | null;
  trading_status: string;
  risk_percent: number;
  ready: boolean;
};

type Props = { apiBaseUrl: string };

const OWNER_SUBSCRIPTIONS_PATH = '/owner/mt5/approvals/subscriptions';

async function readJson<T>(response: Response): Promise<T> {
  let body: unknown = null;
  const contentType = response.headers.get('content-type') || '';
  if (contentType.toLowerCase().includes('json')) {
    try {
      body = await response.json();
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body
      ? (body as { detail: unknown }).detail
      : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : typeof detail === 'string'
        ? detail
        : response.status >= 500
          ? 'Member controls are temporarily unavailable. Please refresh in a moment.'
          : 'The member control could not be completed safely.';
    throw new Error(message);
  }
  if (body === null) {
    throw new Error('Member controls returned an invalid response. Please refresh in a moment.');
  }
  return body as T;
}

function riskLabel(user: ManagedUser): string {
  if (user.risk_percent === null) return 'Not configured';
  return `${user.risk_percent}%${user.allow_double_lot ? ' · Double-lot enabled' : ''}`;
}

function accessLabel(access: MemberAccessState | undefined): string {
  if (!access) return 'Unavailable';
  const plan = access.plan_code === 'complimentary' ? 'Complimentary' : access.plan_code ? access.plan_code : 'No plan';
  return `${plan} · ${access.status}`;
}

export function AdminMemberControlsDay35({ apiBaseUrl }: Props) {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [memberAccess, setMemberAccess] = useState<MemberAccessState[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [accessBusyUserId, setAccessBusyUserId] = useState<string | null>(null);
  const [preview, setPreview] = useState<RevokePreview | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<RevokeResult | null>(null);
  const [reconnectUser, setReconnectUser] = useState<ManagedUser | null>(null);
  const [reconnectPassword, setReconnectPassword] = useState('');
  const [reconnectBusy, setReconnectBusy] = useState(false);
  const [reconnectError, setReconnectError] = useState<string | null>(null);

  const accessByUserId = useMemo(
    () => new Map(memberAccess.map((access) => [access.user_id, access])),
    [memberAccess],
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [usersResponse, accessResponse] = await Promise.all([
        fetch(`${apiBaseUrl}/access/day35/user-controls/users`, {
          credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
        }),
        fetch(`${apiBaseUrl}${OWNER_SUBSCRIPTIONS_PATH}/members`, {
          credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
        }),
      ]);
      const [nextUsers, nextAccess] = await Promise.all([
        readJson<ManagedUser[]>(usersResponse),
        readJson<MemberAccessState[]>(accessResponse),
      ]);
      setUsers(nextUsers);
      setMemberAccess(nextAccess);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Member controls are temporarily unavailable.');
    } finally { setLoading(false); }
  }, [apiBaseUrl]);

  useEffect(() => { void load(); }, [load]);

  async function updateMemberAccess(user: ManagedUser, action: 'pause' | 'resume') {
    setAccessBusyUserId(user.user_id); setError(null); setNotice(null); setResult(null);
    try {
      const response = await fetch(`${apiBaseUrl}${OWNER_SUBSCRIPTIONS_PATH}/users/${user.user_id}/${action}`, {
        method: 'POST', credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
      });
      const completed = await readJson<MemberAccessMutation>(response);
      setNotice(completed.message);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : `Subscription ${action} failed.`);
    } finally { setAccessBusyUserId(null); }
  }

  function openReconnect(user: ManagedUser) {
    setReconnectUser(user);
    setReconnectPassword('');
    setReconnectError(null);
    setNotice(null);
    setError(null);
  }

  async function reconnectMt5() {
    if (!reconnectUser || !reconnectPassword) return;
    setReconnectBusy(true);
    setReconnectError(null);
    setNotice(null);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/accounts/members/${reconnectUser.user_id}/reconnect-mt5`, {
        method: 'POST',
        credentials: 'include',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ mt5_password: reconnectPassword }),
      });
      const completed = await readJson<ReconnectResult>(response);
      if (completed.ready) {
        setNotice(`${completed.display_name} MT5 is connected. Automation is active at ${completed.risk_percent}% risk.`);
        setReconnectUser(null);
        setReconnectPassword('');
      } else {
        setReconnectError(`Broker session is still ${completed.remote_connection_status || completed.mt5_status}. Trading remains stopped.`);
      }
      await load();
    } catch (caught) {
      setReconnectError(caught instanceof Error ? caught.message : 'MT5 reconnect failed safely.');
      await load();
    } finally {
      setReconnectBusy(false);
    }
  }

  async function openRevoke(user: ManagedUser) {
    setError(null); setResult(null); setNotice(null); setConfirmation('');
    try {
      const response = await fetch(`${apiBaseUrl}/access/day35/user-controls/users/${user.user_id}/revoke-preview`, {
        credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
      });
      setPreview(await readJson<RevokePreview>(response));
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Revoke preview is unavailable.'); }
  }

  async function confirmRevoke() {
    if (!preview || confirmation !== preview.confirmation_text) return;
    setExecuting(true); setError(null); setNotice(null);
    try {
      const response = await fetch(`${apiBaseUrl}/access/day35/user-controls/users/${preview.user.user_id}/revoke`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ confirmed: true, confirmation_text: confirmation }),
      });
      const completed = await readJson<RevokeResult>(response);
      setResult(completed); setPreview(null); setConfirmation(''); await load();
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Revoke did not complete safely.'); }
    finally { setExecuting(false); }
  }

  const activeSubscriptions = memberAccess.filter((access) => access.active).length;
  const pausedSubscriptions = memberAccess.filter((access) => access.status === 'suspended').length;

  return <section className="day35-members" aria-labelledby="day35-members-title">
    <div className="workspace-page-header"><div><p className="eyebrow">Owner controls</p><h1 id="day35-members-title">Members &amp; account access</h1><p className="intro">Pause a member when payment is due without deleting their MT5 connection, credentials, settings or history. Resume restores the saved setup. Full revocation remains a separate confirmed action.</p></div><span className="workspace-role-pill">OWNER ONLY</span></div>

    <AdminMemberOnboarding apiBaseUrl={apiBaseUrl} onCompleted={load} />

    {error && <div className="day35-members-error" role="alert">{error}</div>}
    {notice && <div className="day35-members-result" role="status"><strong>Access updated.</strong><span>{notice}</span></div>}
    {result && <div className="day35-members-result" role="status"><strong>Account revoked safely.</strong><span>{result.mapped_positions_closed} mapped position(s) closed · {result.sessions_revoked} session(s) revoked · {result.approvals_revoked} MT5 approval(s) revoked · manual/unmapped positions touched: no.</span></div>}

    <div className="day35-members-summary"><article><span>Managed users</span><strong>{users.length}</strong></article><article><span>Subscriptions active</span><strong>{activeSubscriptions}</strong></article><article><span>Subscriptions paused</span><strong>{pausedSubscriptions}</strong></article><article><span>Automation active</span><strong>{users.filter((user) => user.trading_status === 'active').length}</strong></article></div>

    {loading && !users.length ? <div className="day35-members-loading"><div /><div /></div> : <div className="day35-member-list">{users.map((user) => {
      const access = accessByUserId.get(user.user_id);
      const isPaused = access?.status === 'suspended';
      const canPause = user.status === 'active' && Boolean(access?.active);
      const canResume = user.status === 'active' && isPaused;
      const accessBusy = accessBusyUserId === user.user_id;
      const canReconnectMt5 = user.status === 'active' && Boolean(user.mt5_login_masked) && user.mt5_status !== 'connected';
      return <article className={`day35-member-card day35-member-card--${user.status}${isPaused ? ' day35-member-card--paused' : ''}`} key={user.user_id}>
        <div className="day35-member-head"><div><strong>{user.display_name || user.email}</strong><small>{user.email}</small></div><span>{isPaused ? 'paused' : user.status}</span></div>
        <div className="day35-member-grid"><span><small>Subscription</small><strong>{accessLabel(access)}</strong></span><span><small>Automation</small><strong>{user.trading_status || 'Not configured'}</strong></span><span><small>Risk</small><strong>{riskLabel(user)}</strong></span><span><small>MT5</small><strong>{user.mt5_status || 'Not linked'}</strong></span><span><small>MT5 login</small><strong>{user.mt5_login_masked || '—'}</strong></span><span><small>Mapped open</small><strong>{user.mapped_open_positions}</strong></span><span><small>Mapped pending</small><strong>{user.mapped_pending_positions}</strong></span><span><small>Active sessions</small><strong>{user.active_sessions}</strong></span><span><small>Push devices</small><strong>{user.push_devices_enabled}</strong></span></div>
        <div className="day35-member-actions">
          {canReconnectMt5 && <button type="button" className="day35-resume-button" onClick={() => openReconnect(user)}>Reconnect MT5</button>}
          <button
            type="button"
            className={isPaused ? 'day35-resume-button' : 'day35-pause-button'}
            disabled={accessBusy || (isPaused ? !canResume : !canPause)}
            title={!access ? 'Subscription state unavailable' : !canPause && !canResume ? `Nothing to pause while access is ${access.status}.` : undefined}
            onClick={() => void updateMemberAccess(user, isPaused ? 'resume' : 'pause')}
          >{accessBusy ? 'Updating…' : isPaused ? 'Resume subscription' : 'Pause subscription'}</button>
          {user.status === 'active' && <button type="button" className="day35-revoke-button" onClick={() => void openRevoke(user)}>Review revoke action</button>}
        </div>
      </article>;
    })}</div>}

    {reconnectUser && <div className="day35-confirm-backdrop" role="presentation"><section className="day35-confirm-card" role="dialog" aria-modal="true" aria-labelledby="day35-reconnect-dialog-title">
      <h2 id="day35-reconnect-dialog-title">Reconnect {reconnectUser.display_name || reconnectUser.email}</h2>
      <p>This reconnects the existing live MT5 profile. It does not create another user. Existing account: <strong>{reconnectUser.mt5_login_masked}</strong>{reconnectUser.mt5_server ? <> · {reconnectUser.mt5_server}</> : null}.</p>
      <label>MT5 trading password<input type="password" value={reconnectPassword} onChange={(event) => setReconnectPassword(event.target.value)} autoComplete="new-password" /></label>
      {reconnectError && <div className="day35-members-error" role="alert">{reconnectError}</div>}
      <div className="day35-confirm-actions"><button type="button" className="button button--quiet" onClick={() => { setReconnectUser(null); setReconnectPassword(''); setReconnectError(null); }} disabled={reconnectBusy}>Cancel</button><button type="button" className="day35-resume-button" disabled={reconnectBusy || !reconnectPassword} onClick={() => void reconnectMt5()}>{reconnectBusy ? 'Reconnecting & verifying…' : 'Reconnect MT5'}</button></div>
      <small className="day35-confirm-footnote">The trading password is used only for this broker reconnect request and is not stored by Smart Signals.</small>
    </section></div>}

    {preview && <div className="day35-confirm-backdrop" role="presentation"><section className="day35-confirm-card" role="dialog" aria-modal="true" aria-labelledby="day35-revoke-dialog-title"><div className="day35-confirm-danger">!</div><h2 id="day35-revoke-dialog-title">{preview.confirmation_title}</h2><p>{preview.confirmation_message}</p><div className="day35-confirm-facts"><span><small>Mapped broker positions</small><strong>{preview.mapped_positions_to_close}</strong></span><span><small>Manual/unmapped MT5 positions</small><strong>Excluded</strong></span><span><small>Access after completion</small><strong>Blocked</strong></span></div><label>Type exactly <code>{preview.confirmation_text}</code><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label><div className="day35-confirm-actions"><button type="button" className="button button--quiet" onClick={() => { setPreview(null); setConfirmation(''); }} disabled={executing}>Cancel</button><button type="button" className="day35-danger-action" disabled={executing || confirmation !== preview.confirmation_text} onClick={() => void confirmRevoke()}>{executing ? 'Stopping & revoking…' : 'Stop mapped trades & revoke'}</button></div><small className="day35-confirm-footnote">If broker closure fails, automation stays stopped and the account is not falsely reported as fully revoked.</small></section></div>}

    <div className="day35-members-safety"><span aria-hidden="true">◎</span><div><strong>Pause preserves the account</strong><small>Pausing blocks new subscription-gated trading but keeps the member record, MT5 link, saved settings and history. Existing mapped positions remain available to the normal safety/management path instead of being abandoned.</small></div></div>
  </section>;
}
