import { useCallback, useEffect, useMemo, useState } from 'react';

type TimelineTrade = {
  signal_id: string;
  symbol: string;
  side: string;
  status: string;
  status_label: string;
  status_color: string;
  source_label: string | null;
  trader_stream: string | null;
  source_color_index: number | null;
  opened_at: string | null;
  closed_at: string | null;
  position_count: number;
  open_positions: number;
  pending_positions: number;
  closed_positions: number;
  cash_pnl: number | null;
  net_pips: number | null;
  model_500_pnl: number | null;
  close_reason: string | null;
};

type TimelineData = {
  trades: TimelineTrade[];
  open_count: number;
  pending_count: number;
  provider_identity_visible: boolean;
  broker_trade_action_created: boolean;
};

type LiveAccountState = {
  open: number;
  pending: number | null;
};

type LivePosition = {
  signal_id: string;
  symbol: string;
  side: string;
  volume: number;
  planned_risk_percent: number;
  entry_price: number;
  current_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  profit: number | null;
};

type LiveDashboard = {
  account: { balance: number; currency: string } | null;
  open_positions: LivePosition[];
};

type SignalProjection = {
  current: number | null;
  currentPercent: number | null;
  tp: number | null;
  tpPercent: number | null;
  sl: number | null;
  slPercent: number | null;
  entryPrice: number | null;
  currentPrice: number | null;
  positionCount: number;
};

type FilterKey = 'all' | 'open' | 'pending' | 'closed' | 'won' | 'lost' | 'breakeven' | 'skipped';

type Props = {
  apiBaseUrl: string;
  currency: string;
};

const TRADE_MARKERS = ['🟣', '🟪', '🔷', '🟧', '🔶', '🔹', '🔸', '💠'] as const;

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? (body as { detail: unknown }).detail : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : 'Trade history is temporarily unavailable.';
    throw new Error(message);
  }
  return body;
}

function money(value: number | null, currency: string): string {
  if (value === null || !Number.isFinite(value)) return '—';
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: currency || 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${currency || '$'} ${value.toFixed(2)}`;
  }
}

function signedMoney(value: number | null, currency: string): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return `${value >= 0 ? '+' : '-'}${money(Math.abs(value), currency)}`;
}

function signedPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function shortTime(value: string | null): string {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '—';
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(parsed);
}

function statusIcon(status: string): string {
  if (status === 'open') return '●';
  if (status === 'pending') return '◷';
  if (status === 'won') return '✓';
  if (status === 'lost') return '×';
  if (status === 'breakeven') return '=';
  if (status === 'skipped') return '!';
  return '○';
}

function publicTradeIdentity(signalId: string): { reference: string; marker: string } {
  const compact = signalId.replaceAll('-', '').toUpperCase();
  const markerByte = Number.parseInt(compact.slice(10, 12), 16);
  const markerIndex = Number.isFinite(markerByte) ? markerByte % TRADE_MARKERS.length : 0;
  return {
    reference: `SS-${compact.slice(0, 10)}`,
    marker: TRADE_MARKERS[markerIndex],
  };
}

function pnlClass(value: number | null): string {
  if (value === null || value === 0) return 'is-flat';
  return value > 0 ? 'is-positive' : 'is-negative';
}

function skippedReason(value: string | null): string {
  if (!value) return 'This trade was not placed because a safety check stopped it.';
  return value.replaceAll('_', ' ');
}

function projectedAt(position: LivePosition, target: number | null, balance: number | null, kind: 'tp' | 'sl'): number | null {
  const entry = Number(position.entry_price);
  const level = target === null ? Number.NaN : Number(target);
  const volume = Number(position.volume);
  const symbol = String(position.symbol || '').toUpperCase();
  const side = String(position.side || '').toUpperCase();

  if (Number.isFinite(entry) && Number.isFinite(level) && Number.isFinite(volume) && volume > 0 && symbol === 'XAUUSD' && (side === 'BUY' || side === 'SELL')) {
    const direction = side === 'BUY' ? 1 : -1;
    return (level - entry) * direction * volume * 100;
  }

  if (balance !== null && Number.isFinite(balance) && balance > 0 && Number.isFinite(position.planned_risk_percent)) {
    const riskAmount = balance * Number(position.planned_risk_percent) / 100;
    if (kind === 'sl') return -riskAmount;
    const sl = position.stop_loss === null ? Number.NaN : Number(position.stop_loss);
    const tp = position.take_profit === null ? Number.NaN : Number(position.take_profit);
    if (Number.isFinite(entry) && Number.isFinite(sl) && Number.isFinite(tp)) {
      const riskDistance = Math.abs(entry - sl);
      const rewardDistance = Math.abs(tp - entry);
      if (riskDistance > 0) return riskAmount * rewardDistance / riskDistance;
    }
  }
  return null;
}

function buildProjection(signalId: string, dashboard: LiveDashboard | null): SignalProjection | null {
  const positions = dashboard?.open_positions?.filter((position) => position.signal_id === signalId) ?? [];
  if (positions.length === 0) return null;
  const balance = dashboard?.account && Number.isFinite(dashboard.account.balance) ? dashboard.account.balance : null;
  const profits = positions.map((position) => position.profit).filter((value): value is number => value !== null && Number.isFinite(value));
  const current = profits.length > 0 ? profits.reduce((sum, value) => sum + value, 0) : null;
  const tpValues = positions.map((position) => projectedAt(position, position.take_profit, balance, 'tp'));
  const slValues = positions.map((position) => projectedAt(position, position.stop_loss, balance, 'sl'));
  const tp = tpValues.every((value) => value !== null) ? tpValues.reduce<number>((sum, value) => sum + (value ?? 0), 0) : null;
  const sl = slValues.every((value) => value !== null) ? slValues.reduce<number>((sum, value) => sum + (value ?? 0), 0) : null;
  const firstEntry = positions.find((position) => Number.isFinite(position.entry_price))?.entry_price ?? null;
  const firstCurrent = positions.find((position) => position.current_price !== null && Number.isFinite(position.current_price))?.current_price ?? null;
  return {
    current,
    currentPercent: balance && current !== null ? current / balance * 100 : null,
    tp,
    tpPercent: balance && tp !== null ? tp / balance * 100 : null,
    sl,
    slPercent: balance && sl !== null ? sl / balance * 100 : null,
    entryPrice: firstEntry,
    currentPrice: firstCurrent,
    positionCount: positions.length,
  };
}

export function TradeTimeline({ apiBaseUrl, currency }: Props) {
  const [data, setData] = useState<TimelineData | null>(null);
  const [liveState, setLiveState] = useState<LiveAccountState | null>(null);
  const [liveDashboard, setLiveDashboard] = useState<LiveDashboard | null>(null);
  const [statusFilter, setStatusFilter] = useState<FilterKey>('all');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [traderFilter, setTraderFilter] = useState('all');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);

    try {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      const query = new URLSearchParams({ timezone_name: timezone });
      const [timelineResponse, liveResponse, dashboardResponse] = await Promise.all([
        fetch(`${apiBaseUrl}/account/mt5/dashboard/performance/timeline?limit=250`, {
          credentials: 'include',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        }),
        fetch(`${apiBaseUrl}/account/mt5/dashboard/today?${query.toString()}`, {
          credentials: 'include',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        }),
        fetch(`${apiBaseUrl}/account/mt5/dashboard?${query.toString()}`, {
          credentials: 'include',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        }),
      ]);

      const next = await readJson<TimelineData>(timelineResponse);
      setData(next);
      setError(null);

      if (liveResponse.ok) {
        const state = (await liveResponse.json()) as LiveAccountState;
        setLiveState({ open: state.open, pending: state.pending });
      } else {
        setLiveState(null);
      }
      if (dashboardResponse.ok) {
        const dashboard = (await dashboardResponse.json()) as LiveDashboard;
        setLiveDashboard(dashboard);
      } else {
        setLiveDashboard(null);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Trade history is temporarily unavailable.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    void refresh(true);
    const interval = window.setInterval(() => void refresh(true), 15_000);
    const onFocus = () => void refresh(true);
    const onVisibility = () => { if (document.visibilityState === 'visible') void refresh(true); };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [refresh]);

  const sources = useMemo(() => {
    if (!data?.provider_identity_visible) return [];
    return Array.from(new Set(data.trades.map((trade) => trade.source_label).filter((value): value is string => Boolean(value)))).sort();
  }, [data]);

  const traders = useMemo(() => {
    if (!data?.provider_identity_visible) return [];
    return Array.from(new Set(data.trades.map((trade) => trade.trader_stream).filter((value): value is string => Boolean(value)))).sort();
  }, [data]);

  const visibleTrades = useMemo(() => {
    if (!data) return [];
    const acceptedStatuses: Record<FilterKey, Set<string> | null> = {
      all: null,
      open: new Set(['open']),
      pending: new Set(['pending']),
      closed: new Set(['won', 'lost', 'breakeven']),
      won: new Set(['won']),
      lost: new Set(['lost']),
      breakeven: new Set(['breakeven']),
      skipped: new Set(['skipped']),
    };
    return data.trades.filter((trade) => {
      const statuses = acceptedStatuses[statusFilter];
      if (statuses && !statuses.has(trade.status)) return false;
      if (sourceFilter !== 'all' && trade.source_label !== sourceFilter) return false;
      if (traderFilter !== 'all' && trade.trader_stream !== traderFilter) return false;
      return true;
    });
  }, [data, sourceFilter, statusFilter, traderFilter]);

  if (loading && !data) {
    return <section className="day33-timeline day33-timeline--loading" aria-label="Loading trade history"><div /><div /><div /></section>;
  }

  return <section className="day33-timeline" aria-labelledby="day33-timeline-title">
    <div className="day33-timeline-head">
      <div><span>Your trades</span><h2 id="day33-timeline-title">Trade history</h2><p>Open, pending and completed Smart Signals trades in one place.</p></div>
      <button type="button" className="day33-refresh" onClick={() => void refresh()} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Refresh'}</button>
    </div>

    {error && <div className="day33-sync-note day33-sync-note--error" role="alert">{error}</div>}

    <div className="day33-live-strip" aria-label="Current live broker state">
      <div><span className="day33-live-dot day33-live-dot--open" /><strong>{liveState === null ? '—' : liveState.open}</strong><span>Open</span></div>
      <div><span className="day33-live-dot day33-live-dot--pending" /><strong>{liveState?.pending === null || liveState === null ? '—' : liveState.pending}</strong><span>Pending</span></div>
      <small>Live account state · Pending from MT5</small>
    </div>

    <div className="day33-status-filters" aria-label="Filter trade status">
      {([
        ['all', 'All'], ['open', 'Open'], ['pending', 'Pending'], ['closed', 'Closed'],
        ['won', 'Won'], ['lost', 'Lost'], ['breakeven', 'BE'], ['skipped', 'Skipped'],
      ] as Array<[FilterKey, string]>).map(([key, label]) => <button key={key} type="button" className={statusFilter === key ? 'is-selected' : ''} onClick={() => setStatusFilter(key)}>{label}</button>)}
    </div>

    {data?.provider_identity_visible && (sources.length > 0 || traders.length > 0) && <div className="day33-identity-filters" aria-label="Filter signal source and trader">
      {sources.length > 0 && <label><span>Source</span><select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}><option value="all">All sources</option>{sources.map((source) => <option key={source} value={source}>{source}</option>)}</select></label>}
      {traders.length > 0 && <label><span>Trader</span><select value={traderFilter} onChange={(event) => setTraderFilter(event.target.value)}><option value="all">All traders</option>{traders.map((trader) => <option key={trader} value={trader}>{trader}</option>)}</select></label>}
    </div>}

    {visibleTrades.length === 0 ? <div className="day33-empty"><strong>No trades yet</strong><span>Your Smart Signals trades will appear here.</span></div> : <div className="day33-trade-list">{visibleTrades.map((trade) => {
      const identity = publicTradeIdentity(trade.signal_id);
      const projection = trade.status === 'open' ? buildProjection(trade.signal_id, liveDashboard) : null;
      const displayedPnl = projection?.current ?? trade.cash_pnl;
      return <article className={`day33-trade-card day33-status--${trade.status_color}`} key={trade.signal_id}>
        <div className="day33-trade-reference" aria-label={`Trade ${identity.reference}`}><span aria-hidden="true">{identity.marker}</span><strong>{identity.reference}</strong><small>Trade ID</small></div>
        <div className="day33-trade-top">
          <div className="day33-trade-symbol"><span className={`day32-side day32-side--${trade.side.toLowerCase()}`}>{trade.side}</span><strong>{trade.symbol}</strong></div>
          <span className={`day33-status-badge day33-status-badge--${trade.status_color}`}><i aria-hidden="true">{statusIcon(trade.status)}</i>{trade.status_label}</span>
        </div>

        {data?.provider_identity_visible && trade.source_label && <div className="day33-identity-row">
          <span className={`day33-source-chip day33-source-chip--${trade.source_color_index ?? 0}`}><i />{trade.source_label}</span>
          {trade.trader_stream && <span className={`day33-trader-chip day33-source-chip--${trade.source_color_index ?? 0}`}>{trade.trader_stream}</span>}
        </div>}

        {trade.status === 'skipped' ? <div className="day33-skipped-reason"><strong>Not placed</strong><span>{skippedReason(trade.close_reason)}</span></div> : <>
          <div className="day33-progress-row">
            {trade.open_positions > 0 && <span>{trade.open_positions}/{trade.position_count} positions open</span>}
            {trade.pending_positions > 0 && <span>{trade.pending_positions} pending</span>}
            {trade.closed_positions > 0 && <span>{trade.closed_positions} closed</span>}
          </div>

          {projection && <div className="day33-live-projection" aria-label="Live signal profit and loss projection">
            <div className="day33-live-projection-head"><span><i />LIVE SIGNAL</span><small>{projection.positionCount} position{projection.positionCount === 1 ? '' : 's'} remaining</small></div>
            <div className="day33-live-projection-grid">
              <div><span>Current P/L</span><strong className={pnlClass(projection.current)}>{signedMoney(projection.current, currency)}</strong><small>{signedPercent(projection.currentPercent)}</small></div>
              <div><span>TP estimate</span><strong className={pnlClass(projection.tp)}>{signedMoney(projection.tp, currency)}</strong><small>{signedPercent(projection.tpPercent)}</small></div>
              <div><span>SL estimate</span><strong className={pnlClass(projection.sl)}>{signedMoney(projection.sl, currency)}</strong><small>{signedPercent(projection.slPercent)}</small></div>
            </div>
            <div className="day33-live-projection-prices"><span>Entry {projection.entryPrice === null ? '—' : projection.entryPrice.toFixed(2)}</span><span>Now {projection.currentPrice === null ? '—' : projection.currentPrice.toFixed(2)}</span><em>Auto-calculated from live balance, lot size, SL and TP.</em></div>
          </div>}

          <div className="day33-trade-metrics">
            <div><span>{trade.status === 'open' ? 'Live P/L' : 'Actual P/L'}</span><strong className={pnlClass(displayedPnl)}>{trade.status === 'open' ? signedMoney(displayedPnl, currency) : money(displayedPnl, currency)}</strong></div>
            <div><span>Pips</span><strong>{trade.net_pips === null ? '—' : `${trade.net_pips > 0 ? '+' : ''}${trade.net_pips}`}</strong></div>
            <div><span>$500 @ 1%</span><strong className={pnlClass(trade.model_500_pnl)}>{trade.model_500_pnl === null ? '—' : money(trade.model_500_pnl, 'USD')}</strong></div>
          </div>
        </>}

        <div className="day33-trade-time">{trade.status === 'skipped' ? <span>Skipped {shortTime(trade.closed_at)}</span> : <><span>{trade.opened_at ? `Opened ${shortTime(trade.opened_at)}` : 'Open time unavailable'}</span>{trade.closed_at && <span>Closed {shortTime(trade.closed_at)}</span>}</>}</div>
      </article>;
    })}</div>}

    <div className="day33-legend" aria-label="Trade status colour legend">
      <span><i className="is-blue" />Open</span><span><i className="is-amber" />Pending / skipped</span><span><i className="is-green" />Win</span><span><i className="is-red" />Loss</span><span><i className="is-grey" />BE / closed</span>
    </div>
    <p className="day33-privacy-note">Each trade keeps the same SS Trade ID from opening to final result, so every update is easy to match to the correct trade.{data?.provider_identity_visible ? ' Source and trader labels are shown separately for administrators.' : ''}</p>
  </section>;
}
