import { useCallback, useEffect, useState } from 'react';

import './admin-emergency-stop-day35.css';

type EmergencyPreview = {
  active_users: number;
  automation_active_users: number;
  mapped_open_positions: number;
  confirmation_text: string;
  confirmation_title: string;
  confirmation_message: string;
  manual_or_unmapped_positions_touched: boolean;
  broker_trade_action_created: boolean;
};

type EmergencyResult = {
  users_targeted: number;
  users_completed: number;
  users_failed: number;
  automation_users_stopped_first: number;
  broker_actions_sent: number;
  mapped_positions_closed: number;
  external_positions_reconciled: number;
  failures: Array<{ user_id: string; error_code: string }>;
  manual_or_unmapped_positions_touched: boolean;
};

type Props = { apiBaseUrl: string };

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? (body as { detail: unknown }).detail : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : 'The emergency control could not be completed safely.';
    throw new Error(message);
  }
  return body;
}

export function AdminEmergencyStopDay35({ apiBaseUrl }: Props) {
  const [preview, setPreview] = useState<EmergencyPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [armed, setArmed] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<EmergencyResult | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/access/day35/user-controls/emergency-preview`, {
        credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
      });
      setPreview(await readJson<EmergencyPreview>(response));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Emergency preview is temporarily unavailable.');
    } finally { setLoading(false); }
  }, [apiBaseUrl]);

  useEffect(() => { void load(); }, [load]);

  async function execute() {
    if (!preview || !armed || confirmation !== preview.confirmation_text) return;
    setExecuting(true); setError(null); setResult(null);
    try {
      const response = await fetch(`${apiBaseUrl}/access/day35/user-controls/emergency-stop`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ confirmed: true, confirmation_text: confirmation }),
      });
      const completed = await readJson<EmergencyResult>(response);
      setResult(completed); setArmed(false); setConfirmation(''); await load();
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Emergency stop did not complete safely.'); }
    finally { setExecuting(false); }
  }

  return <section className="day35-emergency" aria-labelledby="day35-emergency-title">
    <div className="workspace-page-header"><div><p className="eyebrow">Day 35 · Emergency control</p><h1 id="day35-emergency-title">Stop Super Signals automation</h1><p className="intro">A deliberate last-resort control for the automated trading layer. It does not suspend or revoke user accounts.</p></div><span className="workspace-role-pill">CONFIRM TWICE</span></div>

    {error && <div className="day35-emergency-error" role="alert">{error}</div>}
    {result && <div className={`day35-emergency-result ${result.users_failed ? 'has-failures' : ''}`} role="status"><strong>{result.users_failed ? 'Emergency stop completed with follow-up required.' : 'Emergency stop completed.'}</strong><span>{result.automation_users_stopped_first} automation control(s) blocked first · {result.mapped_positions_closed} mapped position(s) closed · {result.external_positions_reconciled} externally closed position(s) reconciled · manual/unmapped touched: no.</span>{result.failures.length > 0 && <small>{result.failures.length} user close operation(s) need retry after broker connectivity recovery.</small>}</div>}

    {loading && !preview ? <div className="day35-emergency-loading" /> : preview && <>
      <div className="day35-emergency-panel">
        <div className="day35-emergency-icon" aria-hidden="true">!</div>
        <div className="day35-emergency-copy"><span>Current blast radius</span><h2>{preview.confirmation_title}</h2><p>{preview.confirmation_message}</p></div>
        <div className="day35-emergency-facts"><article><span>Active invited users</span><strong>{preview.active_users}</strong></article><article><span>Automation currently active</span><strong>{preview.automation_active_users}</strong></article><article><span>Mapped open positions</span><strong>{preview.mapped_open_positions}</strong></article><article><span>Manual / unmapped positions</span><strong>Excluded</strong></article></div>
      </div>

      {!armed ? <button type="button" className="day35-arm-button" onClick={() => setArmed(true)}>Arm emergency stop</button> : <div className="day35-emergency-confirm"><div><strong>Emergency stop armed</strong><span>No action has happened yet. Type the exact phrase below to enable the final button.</span></div><label>Type <code>{preview.confirmation_text}</code><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label><div className="day35-emergency-actions"><button type="button" className="button button--quiet" onClick={() => { setArmed(false); setConfirmation(''); }} disabled={executing}>Disarm</button><button type="button" className="day35-emergency-execute" disabled={executing || confirmation !== preview.confirmation_text} onClick={() => void execute()}>{executing ? 'Stopping automation…' : 'Execute emergency stop'}</button></div></div>}
    </>}

    <div className="day35-emergency-sequence"><strong>Locked execution order</strong><ol><li>Block every existing invited-user automation control before any broker close request.</li><li>For each active invited user, load only local open positions that contain a stored broker position ID.</li><li>Intersect those mapped IDs with the broker’s live position IDs and close only the matches.</li><li>Leave user accounts active and record immutable admin audit evidence, including partial failures.</li></ol></div>

    <div className="day35-emergency-safety"><span aria-hidden="true">◎</span><div><strong>This is not “close the whole MT5 account”.</strong><small>It is a Super Signals mapped-position stop. Manual trades and any broker position Super Signals does not own are outside the target set.</small></div></div>
  </section>;
}
