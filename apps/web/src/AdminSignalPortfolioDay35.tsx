import { useCallback, useEffect, useMemo, useState } from 'react';

import './admin-signal-portfolio-day35.css';

type PeriodKey = 'today' | '7d' | 'month' | 'year' | 'all';
type SortKey = 'realized_pnl' | 'return_percent' | 'win_rate' | 'trade_count';

type PortfolioRow = {
  dimension_type: string;
  source_id: string;
  source_label: string;
  trader_stream: string | null;
  source_color_index: number;
  realized_cash_pnl: number;
  open_cash_pnl: number | null;
  open_cash_pnl_known: boolean;
  return_percent: number | null;
  trades_closed: number;
  trades_open: number;
  wins: number;
  losses: number;
  breakeven: number;
  win_rate_percent: number | null;
  net_pips: number | null;
  rank: number;
};

type PortfolioResponse = {
  period_key: PeriodKey;
  period_label: string;
  period_start: string;
  period_end: string;
  rows: PortfolioRow[];
  performance_basis: string;
  provider_identity_visible: boolean;
  broker_trade_action_created: boolean;
};

type Props = {
  apiBaseUrl: string;
};

const PERIODS: Array<[PeriodKey, string]> = [
  ['today', 'Today'],
  ['7d', '7 days'],
  ['month', 'Month'],
  ['year', 'Year'],
  ['all', 'All time'],
];

const SORTS: Array<[SortKey, string]> = [
  ['realized_pnl', 'Realised P/L'],
  ['return_percent', 'Return %'],
  ['win_rate', 'Win rate'],
  ['trade_count', 'Trade count'],
];

function money(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function signedMoney(value: number): string {
  if (!Number.isFinite(value)) return '—';
  const formatted = money(Math.abs(value));
  return `${value > 0 ? '+' : value < 0 ? '-' : ''}${formatted}`;
}

function signedPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function pips(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}`;
}

function tone(value: number | null): string {
  if (value === null || value === 0) return 'is-flat';
  return value > 0 ? 'is-positive' : 'is-negative';
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body
      ? (body as { detail: unknown }).detail
      : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : 'Signal Portfolio is temporarily unavailable.';
    throw new Error(message);
  }
  return body;
}

export function AdminSignalPortfolioDay35({ apiBaseUrl }: Props) {
  const [period, setPeriod] = useState<PeriodKey>('today');
  const [sortBy, setSortBy] = useState<SortKey>('realized_pnl');
  const [data, setData] = useState<PortfolioResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ period, sort_by: sortBy });
      const response = await fetch(`${apiBaseUrl}/account/mt5/dashboard/performance/admin-portfolio?${params.toString()}`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      setData(await readJson<PortfolioResponse>(response));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Signal Portfolio is temporarily unavailable.');
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl, period, sortBy]);

  useEffect(() => { void load(); }, [load]);

  const totals = useMemo(() => {
    const sourceRows = data?.rows.filter((row) => row.dimension_type === 'source') ?? [];
    return {
      sources: sourceRows.length,
      realised: sourceRows.reduce((sum, row) => sum + row.realized_cash_pnl, 0),
      closed: sourceRows.reduce((sum, row) => sum + row.trades_closed, 0),
      open: sourceRows.reduce((sum, row) => sum + row.trades_open, 0),
    };
  }, [data]);

  return <section className="day35-portfolio" aria-labelledby="day35-portfolio-title">
    <div className="workspace-page-header day35-portfolio-header">
      <div>
        <p className="eyebrow">Admin Signal Portfolio</p>
        <h1 id="day35-portfolio-title">Which signals are actually strongest?</h1>
        <p className="intro">Compare approved providers and reliably attributed trader streams using the canonical broker-backed Day 33 ledger.</p>
      </div>
      <span className="workspace-role-pill">BROKER TRUTH</span>
    </div>

    <div className="day35-periods" aria-label="Portfolio period">
      {PERIODS.map(([key, label]) => <button key={key} type="button" className={period === key ? 'is-selected' : ''} onClick={() => setPeriod(key)}>{label}</button>)}
    </div>

    <div className="day35-portfolio-toolbar">
      <label><span>Rank by</span><select value={sortBy} onChange={(event) => setSortBy(event.target.value as SortKey)}>{SORTS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <button type="button" className="day33-refresh" onClick={() => void load()} disabled={loading}>{loading ? 'Refreshing…' : 'Refresh'}</button>
    </div>

    {error && <div className="day33-sync-note day33-sync-note--error" role="alert">{error}</div>}

    <div className="day35-portfolio-summary" aria-label="Signal Portfolio overview">
      <article><span>Sources in period</span><strong>{loading && !data ? '—' : totals.sources}</strong></article>
      <article><span>Reference realised P/L</span><strong className={tone(totals.realised)}>{loading && !data ? '—' : signedMoney(totals.realised)}</strong></article>
      <article><span>Closed legs</span><strong>{loading && !data ? '—' : totals.closed}</strong></article>
      <article><span>Still open</span><strong>{loading && !data ? '—' : totals.open}</strong></article>
    </div>

    {loading && !data ? <div className="day35-portfolio-loading"><div /><div /><div /></div> : data?.rows.length ? <div className="day35-portfolio-list">
      {data.rows.map((row) => <article className={`day35-provider-card day35-source-color--${row.source_color_index}`} key={`${row.dimension_type}:${row.source_id}:${row.trader_stream ?? 'all'}`}>
        <div className="day35-provider-top">
          <div className="day35-provider-name"><span className="day35-source-dot" aria-hidden="true" /><div><strong>{row.trader_stream ? `${row.source_label} · ${row.trader_stream}` : row.source_label}</strong><small>{row.dimension_type === 'trader' ? 'Reliably attributed trader stream' : 'Telegram source overall'}</small></div></div>
          <span className="day35-rank">#{row.rank}</span>
        </div>

        <div className="day35-provider-primary">
          <div><span>Realised P/L</span><strong className={tone(row.realized_cash_pnl)}>{signedMoney(row.realized_cash_pnl)}</strong></div>
          <div><span>Return</span><strong className={tone(row.return_percent)}>{signedPercent(row.return_percent)}</strong></div>
          <div><span>Win rate</span><strong>{row.win_rate_percent === null ? '—' : `${row.win_rate_percent.toFixed(1)}%`}</strong></div>
        </div>

        <div className="day35-provider-secondary">
          <span><small>Won</small><strong>{row.wins}</strong></span>
          <span><small>Lost</small><strong>{row.losses}</strong></span>
          <span><small>BE</small><strong>{row.breakeven}</strong></span>
          <span><small>Closed</small><strong>{row.trades_closed}</strong></span>
          <span><small>Open</small><strong>{row.trades_open}</strong></span>
          <span><small>Net pips</small><strong>{pips(row.net_pips)}</strong></span>
        </div>

        <div className="day35-floating-row"><span>Current floating P/L</span><strong className={row.open_cash_pnl_known ? tone(row.open_cash_pnl) : 'is-unknown'}>{row.open_cash_pnl_known ? money(row.open_cash_pnl) : 'Waiting for live broker read'}</strong></div>
      </article>)}
    </div> : <div className="day33-empty"><strong>No provider performance in this period</strong><span>Choose a wider period or wait for broker-backed trade outcomes.</span></div>}

    <div className="day35-ledger-note"><strong>One performance truth.</strong><span>These figures are read from the Day 33 broker-backed ledger. Telegram claims never become performance data. Source/trader colours are identity only and remain separate from win/loss status colours.</span></div>
  </section>;
}
