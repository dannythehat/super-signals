import { useCallback, useEffect, useState } from 'react';

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

type Props = { apiBaseUrl: string };

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? (body as { detail: unknown }).detail : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : 'The member control could not be completed safely.';
    throw new Error(message);
  }
  return body;
}

function riskLabel(user: ManagedUser): string {
  if (user.risk_percent === null) return 'Not configured';
  return `${user.risk_percent}%${user.allow_double_lot ? ' · Double-lot enabled' : ''}`;
}

export function AdminMemberControlsDay35({ apiBaseUrl }: Props) {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<RevokePreview | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<RevokeResult | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/access/day35/user-controls/users`, {
        credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
      });
      setUsers(await readJson<ManagedUser[]>(response));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Member controls are temporarily unavailable.');
    } finally { setLoading(false); }
  }, [apiBaseUrl]);

  useEffect(() => { void load(); }, [load]);

  async function openRevoke(user: ManagedUser) {
    setError(null); setResult(null); setConfirmation('');
    try {
      const response = await fetch(`${apiBaseUrl}/access/day35/user-controls/users/${user.user_id}/revoke-preview`, {
        credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
      });
      setPreview(await readJson<RevokePreview>(response));
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Revoke preview is unavailable.'); }
  }

  async function confirmRevoke() {
    if (!preview || confirmation !== preview.confirmation_text) return;
    setExecuting(true); setError(null);
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

  return <section className="day35-members" aria-labelledby="day35-members-title">
    <div className="workspace-page-header"><div><p className="eyebrow">Owner controls</p><h1 id="day35-members-title">Members &amp; account access</h1><p className="intro">See invited-user trading state, mapped Smart Signals exposure and private access. Revocation is a confirmed operational action, not a simple delete.</p></div><span className="workspace-role-pill">OWNER ONLY</span></div>

    {error && <div className="day35-members-error" role="alert">{error}</div>}
    {result && <div className="day35-members-result" role="status"><strong>Account revoked safely.</strong><span>{result.mapped_positions_closed} mapped position(s) closed · {result.sessions_revoked} session(s) revoked · {result.approvals_revoked} MT5 approval(s) revoked · manual/unmapped positions touched: no.</span></div>}

    <div className="day35-members-summary"><article><span>Managed users</span><strong>{users.length}</strong></article><article><span>Active</span><strong>{users.filter((user) => user.status === 'active').length}</strong></article><article><span>Mapped open positions</span><strong>{users.reduce((sum, user) => sum + user.mapped_open_positions, 0)}</strong></article><article><span>Automation active</span><strong>{users.filter((user) => user.trading_status === 'active').length}</strong></article></div>

    {loading && !users.length ? <div className="day35-members-loading"><div /><div /></div> : <div className="day35-member-list">{users.map((user) => <article className={`day35-member-card day35-member-card--${user.status}`} key={user.user_id}>
      <div className="day35-member-head"><div><strong>{user.display_name || user.email}</strong><small>{user.email}</small></div><span>{user.status}</span></div>
      <div className="day35-member-grid"><span><small>Automation</small><strong>{user.trading_status || 'Not configured'}</strong></span><span><small>Risk</small><strong>{riskLabel(user)}</strong></span><span><small>MT5</small><strong>{user.mt5_status || 'Not linked'}</strong></span><span><small>MT5 login</small><strong>{user.mt5_login_masked || '—'}</strong></span><span><small>Mapped open</small><strong>{user.mapped_open_positions}</strong></span><span><small>Mapped pending</small><strong>{user.mapped_pending_positions}</strong></span><span><small>Active sessions</small><strong>{user.active_sessions}</strong></span><span><small>Push devices</small><strong>{user.push_devices_enabled}</strong></span></div>
      {user.status === 'active' && <div className="day35-member-actions"><button type="button" className="day35-revoke-button" onClick={() => void openRevoke(user)}>Review revoke action</button></div>}
    </article>)}</div>}

    {preview && <div className="day35-confirm-backdrop" role="presentation"><section className="day35-confirm-card" role="dialog" aria-modal="true" aria-labelledby="day35-revoke-dialog-title"><div className="day35-confirm-danger">!</div><h2 id="day35-revoke-dialog-title">{preview.confirmation_title}</h2><p>{preview.confirmation_message}</p><div className="day35-confirm-facts"><span><small>Mapped broker positions</small><strong>{preview.mapped_positions_to_close}</strong></span><span><small>Manual/unmapped MT5 positions</small><strong>Excluded</strong></span><span><small>Access after completion</small><strong>Blocked</strong></span></div><label>Type exactly <code>{preview.confirmation_text}</code><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label><div className="day35-confirm-actions"><button type="button" className="button button--quiet" onClick={() => { setPreview(null); setConfirmation(''); }} disabled={executing}>Cancel</button><button type="button" className="day35-danger-action" disabled={executing || confirmation !== preview.confirmation_text} onClick={() => void confirmRevoke()}>{executing ? 'Stopping & revoking…' : 'Stop mapped trades & revoke'}</button></div><small className="day35-confirm-footnote">If broker closure fails, automation stays stopped and the account is not falsely reported as fully revoked.</small></section></div>}

    <div className="day35-members-safety"><span aria-hidden="true">◎</span><div><strong>Mapped-only broker safety</strong><small>The close path selects local Smart Signals positions with a stored broker position ID. Manual and unmapped MT5 positions are not closure targets.</small></div></div>
  </section>;
}
