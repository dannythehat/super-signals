import { useCallback, useEffect, useMemo, useState } from 'react';

import './admin-operations-day35.css';

type Trade = {
  signal_id: string;
  public_reference: string;
  public_marker: string;
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
  telegram_root_published: boolean;
};

type Failure = {
  failure_type: string;
  severity: string;
  title: string;
  detail: string;
  failure_code: string | null;
  occurred_at: string;
  signal_id: string | null;
  public_reference: string | null;
  source_label: string | null;
};

type FailureHistory = {
  event_type: string;
  entity_type: string;
  error_code: string | null;
  created_at: string;
};

type OperationsResponse = {
  generated_at: string;
  trades: Trade[];
  current_failures: Failure[];
  recent_failure_history: FailureHistory[];
  current_failure_count: number;
  provider_identity_visible: boolean;
  broker_trade_action_created: boolean;
};

type Props = { apiBaseUrl: string };
type Tab = 'trades' | 'failures' | 'history';

type TradeFilter = 'all' | 'open' | 'pending' | 'won' | 'lost' | 'breakeven' | 'skipped' | 'closed';

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? (body as { detail: unknown }).detail : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : 'Trades and failures are temporarily unavailable.';
    throw new Error(message);
  }
  return body;
}

function when(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);
}

function money(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  const absolute = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: 2 }).format(Math.abs(value));
  return `${value > 0 ? '+' : value < 0 ? '-' : ''}${absolute}`;
}

function pips(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(1)} pips`;
}

function tradeTone(status: string): string {
  if (status === 'won') return 'won';
  if (status === 'lost') return 'lost';
  if (status === 'open') return 'open';
  if (status === 'pending') return 'pending';
  if (status === 'skipped') return 'skipped';
  return 'closed';
}

function eventLabel(value: string): string {
  return value.replaceAll('_', ' ').replaceAll('.', ' · ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function AdminOperationsDay35({ apiBaseUrl }: Props) {
  const [data, setData] = useState<OperationsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('trades');
  const [filter, setFilter] = useState<TradeFilter>('all');
  const [search, setSearch] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/access/day35/operations`, {
        credentials: 'include', headers: { Accept: 'application/json' }, cache: 'no-store',
      });
      setData(await readJson<OperationsResponse>(response));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Trades and failures are temporarily unavailable.');
    } finally { setLoading(false); }
  }, [apiBaseUrl]);

  useEffect(() => { void load(); }, [load]);

  const trades = useMemo(() => {
    const query = search.trim().toLowerCase();
    return (data?.trades ?? []).filter((trade) => {
      if (filter !== 'all') {
        if (filter === 'closed') {
          if (!['won', 'lost', 'breakeven', 'closed_unknown'].includes(trade.status)) return false;
        } else if (trade.status !== filter) return false;
      }
      if (!query) return true;
      return [trade.public_reference, trade.symbol, trade.side, trade.source_label ?? '', trade.trader_stream ?? '', trade.status_label]
        .some((value) => value.toLowerCase().includes(query));
    });
  }, [data, filter, search]);

  return <section className="day35-operations" aria-labelledby="day35-operations-title">
    <div className="workspace-page-header"><div><p className="eyebrow">Day 35 · Operations</p><h1 id="day35-operations-title">Trades &amp; failures</h1><p className="intro">Follow canonical Super Signals trades by permanent public identity and inspect current operational failures without opening database tables or broker tooling.</p></div><span className="workspace-role-pill">READ ONLY</span></div>

    <div className="day35-operations-tabs" role="tablist" aria-label="Operations view"><button type="button" className={tab === 'trades' ? 'is-selected' : ''} onClick={() => setTab('trades')}>Trades <b>{data?.trades.length ?? 0}</b></button><button type="button" className={tab === 'failures' ? 'is-selected' : ''} onClick={() => setTab('failures')}>Current failures <b>{data?.current_failure_count ?? 0}</b></button><button type="button" className={tab === 'history' ? 'is-selected' : ''} onClick={() => setTab('history')}>Failure history <b>{data?.recent_failure_history.length ?? 0}</b></button></div>

    <div className="day35-operations-refresh"><span>{data ? `Snapshot ${when(data.generated_at)}` : 'No snapshot yet'}</span><button type="button" onClick={() => void load()} disabled={loading}>{loading ? 'Refreshing…' : 'Refresh'}</button></div>
    {error && <div className="day35-operations-error" role="alert">{error}</div>}

    {tab === 'trades' && <>
      <div className="day35-trade-tools"><div>{(['all', 'open', 'pending', 'won', 'lost', 'breakeven', 'skipped', 'closed'] as TradeFilter[]).map((value) => <button key={value} type="button" className={filter === value ? 'is-selected' : ''} onClick={() => setFilter(value)}>{value}</button>)}</div><input aria-label="Search trades" placeholder="Search reference, source or symbol" value={search} onChange={(event) => setSearch(event.target.value)} /></div>
      {loading && !data ? <div className="day35-operations-loading"><div /><div /><div /></div> : trades.length ? <div className="day35-trade-list">{trades.map((trade) => <article className={`day35-trade-card day35-trade-card--${tradeTone(trade.status)}`} key={trade.signal_id}>
        <div className="day35-trade-head"><div className="day35-trade-id"><span>{trade.public_marker}</span><div><strong>{trade.public_reference}</strong><small>{trade.source_label ?? 'Provider hidden'}{trade.trader_stream ? ` · ${trade.trader_stream}` : ''}</small></div></div><span className="day35-trade-status">{trade.status_label}</span></div>
        <div className="day35-trade-instrument"><strong>{trade.symbol} {trade.side}</strong><span>Opened {when(trade.opened_at)}</span></div>
        <div className="day35-trade-metrics"><span><small>Positions</small><strong>{trade.position_count}</strong></span><span><small>Open</small><strong>{trade.open_positions}</strong></span><span><small>Pending</small><strong>{trade.pending_positions}</strong></span><span><small>Closed</small><strong>{trade.closed_positions}</strong></span><span><small>Realised P/L</small><strong>{money(trade.cash_pnl)}</strong></span><span><small>Net pips</small><strong>{pips(trade.net_pips)}</strong></span></div>
        <div className="day35-trade-foot"><span>Telegram root {trade.telegram_root_published ? 'published' : 'not published'}</span>{trade.close_reason && <span>Close: {trade.close_reason}</span>}{trade.closed_at && <span>Closed {when(trade.closed_at)}</span>}</div>
      </article>)}</div> : <div className="day35-operations-empty"><strong>No trades match this view</strong><span>Change the status filter or search.</span></div>}
    </>}

    {tab === 'failures' && <>{data?.current_failures.length ? <div className="day35-failure-list">{data.current_failures.map((failure, index) => <article className={`day35-failure-card day35-failure-card--${failure.severity}`} key={`${failure.failure_type}:${failure.occurred_at}:${index}`}><span className="day35-failure-dot" aria-hidden="true" /><div><div className="day35-failure-head"><strong>{failure.title}</strong><small>{when(failure.occurred_at)}</small></div><p>{failure.detail}</p><div className="day35-failure-meta">{failure.public_reference && <span>{failure.public_reference}</span>}{failure.source_label && <span>{failure.source_label}</span>}{failure.failure_code && <code>{failure.failure_code}</code>}<span>{failure.failure_type}</span></div></div></article>)}</div> : <div className="day35-operations-clear"><span aria-hidden="true">✓</span><div><strong>No current persisted failures</strong><small>Historic failures remain available in the history tab for audit context.</small></div></div>}</>}

    {tab === 'history' && <>{data?.recent_failure_history.length ? <div className="day35-history-list">{data.recent_failure_history.map((event, index) => <article key={`${event.created_at}:${event.event_type}:${index}`}><span /><div><strong>{eventLabel(event.event_type)}</strong><small>{event.entity_type} · {when(event.created_at)}</small></div>{event.error_code && <code>{event.error_code}</code>}</article>)}</div> : <div className="day35-operations-empty"><strong>No recent failure history</strong><span>The audit history window is currently clear.</span></div>}</>}

    <div className="day35-operations-safety"><span aria-hidden="true">◎</span><div><strong>Canonical read model only</strong><small>Trade status and P/L come from the Day 33 reference execution ledger. This screen cannot place, modify, close, approve or publish a trade.</small></div></div>
  </section>;
}
