import { useCallback, useEffect, useState } from 'react';

type Account = { role: 'owner' | 'trading_admin' | 'user' };
type TradingSettings = {
  risk_percent: number;
  allow_double_lot: boolean;
  effective_normal_risk_percent: number;
  effective_double_lot_risk_percent: number;
  trading_status: 'stopped' | 'active';
  recommended_risk_percent: number;
  recommended_allow_double_lot: boolean;
  allowed_risk_percents: number[];
};
type Requirement = { key: string; label: string; passed: boolean };
type ActivationPreview = {
  settings: TradingSettings;
  ready: boolean;
  requirements: Requirement[];
  confirmation_title: string;
  confirmation_message: string;
};
type StopResult = {
  settings: TradingSettings;
  broker_actions_sent: number;
  positions_closed: number;
  external_positions_reconciled: number;
  already_stopped: boolean;
};
type Mt5ConnectionStatus = { configured: boolean; status: string };
type Mt5OnboardingStatus = { connection_status: string };

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? '/api';
const allowedRisks = [0.5, 1, 1.5, 2];

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? (body as { detail: unknown }).detail : 'Something went wrong.';
    const message = typeof detail === 'object' && detail !== null && 'message' in detail ? String((detail as { message: unknown }).message) : String(detail);
    throw new Error(message);
  }
  return body;
}

export function TradingActivationPanel() {
  const [eligible, setEligible] = useState(false);
  const [settings, setSettings] = useState<TradingSettings | null>(null);
  const [realMt5Status, setRealMt5Status] = useState<Mt5ConnectionStatus | null>(null);
  const [onboardingStatus, setOnboardingStatus] = useState<Mt5OnboardingStatus | null>(null);
  const [preview, setPreview] = useState<ActivationPreview | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const meResponse = await fetch(`${apiBaseUrl}/auth/me`, { credentials: 'include', headers: { Accept: 'application/json' } });
      if (!meResponse.ok) return;
      const account = await readJson<Account>(meResponse);
      if (account.role !== 'user') return;
      setEligible(true);
      const [settingsResponse, mt5Response, onboardingResponse] = await Promise.all([
        fetch(`${apiBaseUrl}/account/mt5/trading`, { credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store' }),
        fetch(`${apiBaseUrl}/account/mt5/status`, { credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store' }),
        fetch(`${apiBaseUrl}/account/mt5/onboarding`, { credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store' }),
      ]);
      setSettings(await readJson<TradingSettings>(settingsResponse));
      setRealMt5Status(await readJson<Mt5ConnectionStatus>(mt5Response));
      setOnboardingStatus(await readJson<Mt5OnboardingStatus>(onboardingResponse));
    } catch {
      // The core App owns global connectivity messages. Keep this user-only panel quiet until available.
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onMt5Connected = () => void refresh();
    window.addEventListener('super-signals-mt5-connected', onMt5Connected);
    return () => window.removeEventListener('super-signals-mt5-connected', onMt5Connected);
  }, [refresh]);

  async function saveRisk(riskPercent: number, allowDoubleLot: boolean) {
    setBusy(true); setNotice(null); setPreview(null);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/trading/risk`, {
        method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ risk_percent: riskPercent, allow_double_lot: allowDoubleLot }),
      });
      const next = await readJson<TradingSettings>(response);
      setSettings(next);
      setNotice(next.trading_status === 'stopped' ? 'Risk settings saved.' : null);
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Risk settings could not be saved.'); }
    finally { setBusy(false); }
  }

  async function prepareActivation() {
    if (realMt5Status?.status !== 'connected') return;
    setBusy(true); setNotice(null); setConfirmStop(false);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/trading/activation-preview`, { credentials: 'include', headers: { Accept: 'application/json' } });
      setPreview(await readJson<ActivationPreview>(response));
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Activation check failed.'); }
    finally { setBusy(false); }
  }

  async function confirmActivation() {
    if (realMt5Status?.status !== 'connected') return;
    setBusy(true); setNotice(null);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/trading/activate`, {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ confirmed: true }),
      });
      setSettings(await readJson<TradingSettings>(response)); setPreview(null); setNotice('Automated trading is active.');
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Trading could not be activated.'); }
    finally { setBusy(false); }
  }

  async function stopAndClose() {
    setBusy(true); setNotice(null);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/trading/stop-and-close`, { method: 'POST', credentials: 'include', headers: { Accept: 'application/json' } });
      const result = await readJson<StopResult>(response);
      setSettings(result.settings); setConfirmStop(false); setPreview(null);
      setNotice(`Trading stopped. ${result.positions_closed} bot position${result.positions_closed === 1 ? '' : 's'} closed.`);
    } catch (error) {
      setConfirmStop(false); setNotice(error instanceof Error ? `${error.message} New trades remain stopped.` : 'New trades remain stopped.');
      await refresh();
    } finally { setBusy(false); }
  }

  if (!eligible || !settings) return null;

  const effectiveDouble = settings.allow_double_lot ? `${settings.effective_double_lot_risk_percent}%` : 'Off';
  const realMt5Connected = realMt5Status?.status === 'connected';
  const acceptanceMirror = onboardingStatus?.connection_status === 'connected' && !realMt5Connected;

  return <aside id="trading-risk-settings" className="trading-activation-panel" aria-label="Automated trading controls">
    <div className="trading-activation-heading">
      <div><span className="status-label">My trading</span><strong>Risk &amp; automation</strong></div>
      <span className={`trading-state trading-state--${settings.trading_status}`}>{settings.trading_status === 'active' ? 'ACTIVE' : 'STOPPED'}</span>
    </div>

    <div className="recommended-preset"><strong>Recommended</strong><span>1% per position · Double-lot signals ON</span></div>

    <fieldset className="risk-selector" disabled={busy}>
      <legend>Risk per position</legend>
      <div className="risk-options">{allowedRisks.map((risk) => <button key={risk} type="button" className={Number(settings.risk_percent) === risk ? 'risk-option risk-option--selected' : 'risk-option'} aria-pressed={Number(settings.risk_percent) === risk} onClick={() => void saveRisk(risk, settings.allow_double_lot)}>{risk}%</button>)}</div>
    </fieldset>

    <label className="double-lot-toggle"><input type="checkbox" checked={settings.allow_double_lot} disabled={busy} onChange={(event) => void saveRisk(Number(settings.risk_percent), event.currentTarget.checked)} /><span><strong>Allow double-lot signals</strong><small>If a provider explicitly says DOUBLE LOTSIZE, Super Signals doubles your selected risk for that position.</small></span></label>

    <div className="effective-risk"><span>Normal signal <strong>{settings.effective_normal_risk_percent}%</strong></span><span>Double-lot signal <strong>{effectiveDouble}</strong></span></div>

    {preview && <div className="activation-confirmation" role="dialog" aria-label={preview.confirmation_title}><strong>{preview.confirmation_title}</strong><p>{preview.confirmation_message}</p><ul>{preview.requirements.map((item) => <li key={item.key} className={item.passed ? 'requirement-pass' : 'requirement-fail'}>{item.passed ? '✓' : '×'} {item.label}</li>)}</ul>{preview.ready ? <div className="control-actions"><button className="button" type="button" disabled={busy || !realMt5Connected} onClick={() => void confirmActivation()}>Confirm &amp; activate</button><button className="button button--quiet" type="button" disabled={busy} onClick={() => setPreview(null)}>Cancel</button></div> : <button className="button button--quiet" type="button" onClick={() => setPreview(null)}>Close</button>}</div>}

    {confirmStop && <div className="activation-confirmation activation-confirmation--danger"><strong>Stop automated trading and close bot positions?</strong><p>New trades will be blocked immediately. Super Signals will close only positions it opened and will not touch manual MT5 positions.</p><div className="control-actions"><button className="button" type="button" disabled={busy} onClick={() => void stopAndClose()}>Stop and close</button><button className="button button--quiet" type="button" disabled={busy} onClick={() => setConfirmStop(false)}>Cancel</button></div></div>}

    {!preview && !confirmStop && settings.trading_status !== 'active' && acceptanceMirror && <div className="settings-notice" role="status"><strong>Read-only test account.</strong> Trading activation is intentionally disabled while this user mirrors the Owner MT5 connection for acceptance testing.</div>}
    {!preview && !confirmStop && settings.trading_status !== 'active' && !acceptanceMirror && !realMt5Connected && <div className="settings-notice" role="status">Connect your approved Vantage MT5 account below before activating automated trading.</div>}
    {!preview && !confirmStop && <div className="control-actions">{settings.trading_status === 'active' ? <button className="button" type="button" disabled={busy} onClick={() => setConfirmStop(true)}>Stop and Close</button> : !acceptanceMirror && realMt5Connected ? <button className="button" type="button" disabled={busy} onClick={() => void prepareActivation()}>Activate Trades</button> : null}</div>}
    {notice && <p className="trading-control-notice" role="status">{notice}</p>}
  </aside>;
}
