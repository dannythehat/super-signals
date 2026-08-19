import { useCallback, useEffect, useMemo, useState } from 'react';

import { ManualMt5ActivityDay36 } from './ManualMt5ActivityDay36';
import { OwnerCloseAllButton, OwnerPositionCloseButton } from './OwnerInlineCloseControls';
import { TodayTradingSummary } from './TodayTradingSummary';
import { TradeTimeline } from './TradeTimeline';

type DashboardConnection = {
  configured: boolean;
  status: string;
  account_environment: string | null;
  login_masked: string | null;
  server: string | null;
  error_code: string | null;
  read_at: string | null;
};
type DashboardAccount = {
  currency: string;
  balance: number;
  equity: number;
  margin: number;
  free_margin: number;
  trade_allowed: boolean;
};
type DashboardTrading = {
  available: boolean;
  status: string | null;
  risk_percent: number | null;
  allow_double_lot: boolean | null;
  effective_double_lot_risk_percent: number | null;
};
type DashboardPerformance = {
  key: string;
  label: string;
  amount: number | null;
  known_position_count: number;
  provisional_until_day33: boolean;
};
type DashboardPosition = {
  position_id: string;
  broker_position_id: string;
  signal_id: string;
  tp_index: number;
  symbol: string;
  side: string;
  volume: number;
  planned_risk_percent: number;
  entry_price: number;
  current_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  profit: number | null;
  opened_at: string | null;
};
type LatestSignal = {
  signal_id: string;
  symbol: string;
  side: string;
  created_at: string;
  position_count: number;
  open_positions: number;
  closed_positions: number;
  status: string;
};
type CompletedPosition = {
  position_id: string;
  signal_id: string;
  tp_index: number;
  symbol: string;
  side: string;
  closed_at: string | null;
  pnl_amount: number | null;
  close_reason: string | null;
};
type WinLoss = {
  wins: number;
  losses: number;
  breakeven: number;
  known_results: number;
  win_rate_percent: number | null;
};
type ActivityItem = {
  event_type: string;
  label: string;
  tone: string;
  created_at: string;
};
type DashboardData = {
  connection: DashboardConnection;
  account: DashboardAccount | null;
  trading: DashboardTrading;
  open_profit: number | null;
  open_positions: DashboardPosition[];
  latest_signal: LatestSignal | null;
  recent_completed: CompletedPosition[];
  performance: DashboardPerformance[];
  win_loss: WinLoss;
  activity: ActivityItem[];
  reconciled_external_positions: number;
  canonical_performance_ready: boolean;
  performance_basis: string;
  broker_trade_action_created: boolean;
};

type Props = {
  apiBaseUrl: string;
  displayName: string;
  roleLabel: string;
  onOpenSettings: () => void;
};

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? (body as { detail: unknown }).detail : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail ? String((detail as { message: unknown }).message) : 'Account data is temporarily unavailable.';
    throw new Error(message);
  }
  return body;
}

function money(value: number | null | undefined, currency: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
  } catch {
    return `${currency || '$'} ${value.toFixed(2)}`;
  }
}

function price(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 5 }).format(value);
}

function shortTime(value: string | null): string {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '—';
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(parsed);
}

function pnlClass(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return 'is-flat';
  return value > 0 ? 'is-positive' : 'is-negative';
}

function connectionCopy(connection: DashboardConnection) {
  if (connection.status === 'connected') return { label: 'MT5 connected', tone: 'connected' };
  if (!connection.configured || connection.status === 'not_configured') return { label: 'MT5 setup needed', tone: 'attention' };
  if (connection.status === 'connection_error') return { label: 'MT5 needs attention', tone: 'attention' };
  return { label: 'MT5 reconnecting', tone: 'attention' };
}

function tradingCopy(data: DashboardData) {
  if (data.trading.available) {
    return data.trading.status === 'active'
      ? { label: 'Trades active', tone: 'active' }
      : { label: 'Trades stopped', tone: 'stopped' };
  }
  if (data.connection.account_environment === 'demo') return { label: 'Demo preview', tone: 'demo' };
  return { label: 'Automation not configured', tone: 'stopped' };
}

export function MobileDashboard({ apiBaseUrl, displayName, roleLabel, onOpenSettings }: Props) {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/dashboard`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      const next = await readJson<DashboardData>(response);
      setData(next);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Account data is temporarily unavailable.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    void refresh(true);
    const interval = window.setInterval(() => void refresh(true), 30000);
    const onFocus = () => void refresh(true);
    const onLedgerSynced = () => void refresh(true);
    window.addEventListener('focus', onFocus);
    window.addEventListener('super-signals-ledger-synced', onLedgerSynced);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('super-signals-ledger-synced', onLedgerSynced);
    };
  }, [refresh]);

  const currency = data?.account?.currency || 'USD';
  const connection = useMemo(() => data ? connectionCopy(data.connection) : null, [data]);
  const trading = useMemo(() => data ? tradingCopy(data) : null, [data]);

  if (loading && !data) {
    return <section className="day32-dashboard day32-dashboard--loading" aria-label="Loading account dashboard" aria-live="polite">
      <div className="day32-loading-hero" />
      <div className="day32-loading-grid"><span /><span /><span /></div>
      <div className="day32-loading-panel" />
    </section>;
  }

  if (!data) {
    return <section className="day32-dashboard day32-dashboard--error" aria-live="polite">
      <span className="status-label">Super Signals home</span>
      <h1>Account view unavailable</h1>
      <p>{error ?? 'The secure account service could not be reached.'}</p>
      <button className="button" type="button" onClick={() => void refresh()} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Try again'}</button>
      <button className="button button--quiet" type="button" onClick={onOpenSettings}>Open Settings</button>
    </section>;
  }

  const accountEnvironment = data.connection.account_environment === 'demo' ? 'Demo account' : data.connection.account_environment === 'live' ? 'Live account' : 'Account';
  const riskText = data.trading.risk_percent === null ? 'Not configured' : `${data.trading.risk_percent}% per TP position`;
  const doubleLotText = data.trading.allow_double_lot === null ? null : data.trading.allow_double_lot ? `Double-lot ON · ${data.trading.effective_double_lot_risk_percent}% effective` : 'Double-lot OFF';
  const memberKicker = roleLabel.toLowerCase().includes('user') ? 'Your account' : roleLabel;
  const isOwnerDemo = roleLabel.toLowerCase().includes('owner') && data.connection.account_environment === 'demo';

  return <section className="day32-dashboard" aria-labelledby="day32-home-title">
    <div className="day32-dashboard-head">
      <div><span className="day32-kicker">{memberKicker}</span><h1 id="day32-home-title">Hi, {displayName}</h1><p>Your Super Signals account, trading state and latest activity.</p></div>
      <button className="day32-settings-button" type="button" onClick={onOpenSettings} aria-label="Open Settings">⚙</button>
    </div>

    {error && <div className="day32-inline-warning" role="status"><span>Live refresh paused</span><strong>{error}</strong><button type="button" onClick={() => void refresh()} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Retry'}</button></div>}

    <div className="day32-state-row" aria-label="Account state">
      <span className={`day32-state-pill day32-state-pill--${trading?.tone ?? 'stopped'}`}><i />{trading?.label}</span>
      <span className={`day32-state-pill day32-state-pill--${connection?.tone ?? 'attention'}`}><i />{connection?.label}</span>
      <span className="day32-account-kind">{accountEnvironment}</span>
    </div>

    <section className="day32-balance-card" aria-label="MT5 account balance">
      <div className="day32-balance-copy"><span>Balance</span><strong>{money(data.account?.balance, currency)}</strong><small>{data.connection.login_masked ? `${data.connection.login_masked} · ${data.connection.server ?? 'Vantage MT5'}` : 'Connect your Vantage MT5 account in Settings'}</small></div>
      <div className="day32-equity-copy"><span>Equity</span><strong>{money(data.account?.equity, currency)}</strong><small>Free margin {money(data.account?.free_margin, currency)}</small></div>
      <button className="day32-refresh" type="button" onClick={() => void refresh()} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Refresh'}</button>
    </section>

    <TodayTradingSummary apiBaseUrl={apiBaseUrl} currency={currency} />

    <div className="day32-performance-grid" aria-label="Performance summary">
      {data.performance.map((period) => {
        const hasPeriodValue = data.canonical_performance_ready && period.amount !== null;
        return <article className="day32-performance-card" key={period.key}><span>{period.label}</span><strong className={pnlClass(hasPeriodValue ? period.amount : null)}>{hasPeriodValue ? money(period.amount, currency) : '—'}</strong><small>{!data.canonical_performance_ready ? 'Updating trade history' : period.known_position_count === 0 ? 'No completed trade yet' : `${period.known_position_count} completed position${period.known_position_count === 1 ? '' : 's'}`}</small></article>;
      })}
    </div>

    <TradeTimeline apiBaseUrl={apiBaseUrl} currency={currency} />

    <section className="day32-risk-strip" aria-label="Selected trading risk">
      <div><span>Selected risk</span><strong>{riskText}</strong>{doubleLotText && <small>{doubleLotText}</small>}</div>
      {data.trading.available && <button type="button" onClick={onOpenSettings}>Manage</button>}
    </section>

    <section className="day32-section" aria-labelledby="open-positions-title">
      <div className="day32-section-head"><div><span>Live account</span><h2 id="open-positions-title">Open positions</h2></div><strong className={`day32-open-profit ${pnlClass(data.open_profit)}`}>{money(data.open_profit, currency)}</strong></div>
      {data.open_positions.length === 0 ? <div className="day32-empty"><strong>No active Super Signals positions</strong><span>Trades placed by Super Signals will appear here when they are open.</span></div> : <>
        <div className="day32-position-list">{data.open_positions.map((position) => <article className="day32-position-card" key={position.position_id}>
          <div className="day32-position-title"><div><span className={`day32-side day32-side--${position.side.toLowerCase()}`}>{position.side}</span><strong>{position.symbol}</strong><small>TP{position.tp_index} · {position.volume} lots</small></div><strong className={pnlClass(position.profit)}>{money(position.profit, currency)}</strong></div>
          <dl><div><dt>Entry</dt><dd>{price(position.entry_price)}</dd></div><div><dt>Now</dt><dd>{price(position.current_price)}</dd></div><div><dt>SL</dt><dd>{price(position.stop_loss)}</dd></div><div><dt>TP</dt><dd>{price(position.take_profit)}</dd></div></dl>
          <div className="owner-position-card-footer"><small className="day32-position-risk">Risk {position.planned_risk_percent}% · Opened {shortTime(position.opened_at)}</small>{isOwnerDemo && <OwnerPositionCloseButton apiBaseUrl={apiBaseUrl} positionId={position.position_id} symbol={position.symbol} tpIndex={position.tp_index} />}</div>
        </article>)}</div>
        {isOwnerDemo && <OwnerCloseAllButton apiBaseUrl={apiBaseUrl} openCount={data.open_positions.length} />}
      </>}
    </section>

    <div className="day32-two-column">
      <section className="day32-section" aria-labelledby="latest-signal-title"><div className="day32-section-head"><div><span>Most recent</span><h2 id="latest-signal-title">Latest trade</h2></div></div>{data.latest_signal ? <article className="day32-latest-signal"><div><span className={`day32-side day32-side--${data.latest_signal.side.toLowerCase()}`}>{data.latest_signal.side}</span><strong>{data.latest_signal.symbol}</strong></div><p>{data.latest_signal.status === 'active' ? `${data.latest_signal.open_positions} of ${data.latest_signal.position_count} positions active` : data.latest_signal.status === 'closed' ? 'Trade completed' : 'Processing'}</p><small>{shortTime(data.latest_signal.created_at)}</small></article> : <div className="day32-empty day32-empty--compact"><strong>No trade yet</strong><span>Your latest Super Signals trade will appear here.</span></div>}</section>

      <section className="day32-section" aria-labelledby="snapshot-title"><div className="day32-section-head"><div><span>Results</span><h2 id="snapshot-title">Win / loss snapshot</h2></div></div><div className="day32-winloss"><div><strong>{data.canonical_performance_ready ? data.win_loss.wins : '—'}</strong><span>Wins</span></div><div><strong>{data.canonical_performance_ready ? data.win_loss.losses : '—'}</strong><span>Losses</span></div><div><strong>{!data.canonical_performance_ready ? '—' : data.win_loss.win_rate_percent === null ? '0%' : `${data.win_loss.win_rate_percent}%`}</strong><span>Win rate</span></div></div>{!data.canonical_performance_ready && <small className="day32-ledger-note">Trade history is updating. Super Signals will not show an estimated result.</small>}</section>
    </div>

    <section className="day32-section" aria-labelledby="recent-trades-title"><div className="day32-section-head"><div><span>Account history</span><h2 id="recent-trades-title">Recent completed positions</h2></div></div>{data.recent_completed.length === 0 ? <div className="day32-empty day32-empty--compact"><strong>No completed Super Signals positions yet</strong></div> : <div className="day32-completed-list">{data.recent_completed.map((item) => <article key={item.position_id}><div><span className={`day32-side day32-side--${item.side.toLowerCase()}`}>{item.side}</span><strong>{item.symbol}</strong><small>TP{item.tp_index} · {shortTime(item.closed_at)}</small></div><strong className={pnlClass(item.pnl_amount)}>{item.pnl_amount === null ? 'Closed' : money(item.pnl_amount, currency)}</strong></article>)}</div>}</section>

    <ManualMt5ActivityDay36 apiBaseUrl={apiBaseUrl} />

    <section className="day32-section" aria-labelledby="activity-title"><div className="day32-section-head"><div><span>Super Signals</span><h2 id="activity-title">Recent activity</h2></div></div>{data.activity.length === 0 ? <div className="day32-empty day32-empty--compact"><strong>No account activity to show yet</strong></div> : <ol className="day32-activity-list">{data.activity.map((item, index) => <li key={`${item.event_type}-${item.created_at}-${index}`}><i className={`day32-activity-dot day32-activity-dot--${item.tone}`} /><div><strong>{item.label}</strong><small>{shortTime(item.created_at)}</small></div></li>)}</ol>}</section>

    <p className="day32-data-note">Your dashboard shows Super Signals trading activity only. Manual MT5 positions stay separate.</p>
  </section>;
}
