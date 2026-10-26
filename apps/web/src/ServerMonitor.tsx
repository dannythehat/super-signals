import { useCallback, useEffect, useState } from 'react';
import './server-monitor.css';

type Position = {
  id: string; symbol: string; side: string; volume: number | null;
  entry_price: number | null; current_price: number | null; profit: number | null;
  stop_loss: number | null; take_profit: number | null; client_id: string;
};
type Broker = {
  profile: string; worker_name: string | null; worker_online: boolean;
  worker_last_seen: string | null; balance: number | null; equity: number | null;
  free_margin: number | null; currency: string | null; trade_allowed: boolean | null;
  login: number | null; server: string | null; account_updated_at: string | null;
  positions_updated_at: string | null; positions: Position[];
  gold: { symbol: string; bid: number; ask: number; updated_at: string } | null;
};
type Monitor = {
  read_at: string; pu_prime: Broker; vantage: Broker;
  mirror: { mode: string; forward_only_since: string; target_login: string };
  source: string;
};
type Props = { apiBaseUrl: string; active: boolean };

const currency = (value: number | null | undefined) =>
  value === null || value === undefined || !Number.isFinite(Number(value))
    ? '—' : new Intl.NumberFormat('en-GB', {
        style: 'currency', currency: 'USD', minimumFractionDigits: 2,
      }).format(Number(value));

const amount = (value: number | null | undefined, places = 2) =>
  value === null || value === undefined || !Number.isFinite(Number(value))
    ? '—' : Number(value).toFixed(places);
const tone = (value: number | null | undefined) =>
  value === null || value === undefined ? '' : value > 0 ? 'ssm-up' : value < 0 ? 'ssm-down' : '';
function age(time: string | null | undefined) {
  if (!time) return 'No recent update';
  const difference = Math.max(0, Math.round((Date.now() - Date.parse(time)) / 1000));
  if (!Number.isFinite(difference)) return 'Time unavailable';
  if (difference < 60) return String(difference) + 's ago';
  return String(Math.floor(difference / 60)) + 'm ago';
}
function WorkerCard({ broker, heading, note }: { broker: Broker; heading: string; note: string }) {
  const floating = broker.equity !== null && broker.balance !== null ? broker.equity - broker.balance : null;
  const accountRecent = broker.account_updated_at
    && Date.now() - Date.parse(broker.account_updated_at) < 90_000;
  return <article className="ssm-card ssm-broker">
    <div className="ssm-head">
      <div><span className="ssm-eyebrow">{note}</span><h2>{heading}</h2></div>
      <span className={'ssm-pill ' + (broker.worker_online ? 'ssm-ok' : 'ssm-off')}>
        {broker.worker_online ? 'Worker online' : 'Worker offline'}
      </span>
    </div>
    <div className="ssm-balance">
      <span>Broker balance</span><strong>{currency(broker.balance)}</strong>
      <small>{accountRecent ? 'Live broker snapshot' : 'Snapshot unavailable or stale'} · {age(broker.account_updated_at)}</small>
    </div>
    <div className="ssm-metrics">
      <div><span>Equity</span><strong>{currency(broker.equity)}</strong></div>
      <div><span>Open P&amp;L</span><strong className={tone(floating)}>{currency(floating)}</strong></div>
      <div><span>Open positions</span><strong>{broker.positions_updated_at ? broker.positions.length : '—'}</strong></div>
    </div>
    <p className="ssm-meta">{broker.login ?? 'MT5'} · {broker.server || 'Server unavailable'} · Updated {age(broker.worker_last_seen)}</p>
  </article>;
}
function TradeRows({ broker, heading }: { broker: Broker; heading: string }) {
  return <section className="ssm-card ssm-trades" aria-label={heading}>
    <div className="ssm-head"><h2>{heading}</h2><span className="ssm-light">{age(broker.positions_updated_at)}</span></div>
    {!broker.positions_updated_at && <p className="ssm-empty">No recent position snapshot.</p>}
    {broker.positions_updated_at && broker.positions.length === 0 && <p className="ssm-empty">No open positions.</p>}
    {broker.positions.map(p => <article className="ssm-trade" key={p.id}>
      <div><strong className={p.side === 'BUY' ? 'ssm-buy' : 'ssm-sell'}>{p.side} {p.symbol}</strong>
        <small>{amount(p.volume)} lots · Entry {amount(p.entry_price)}</small></div>
      <div><strong className={tone(p.profit)}>{currency(p.profit)}</strong><small>Now {amount(p.current_price)}</small></div>
      <div className="ssm-targets">SL {amount(p.stop_loss)} <span>·</span> TP {amount(p.take_profit)}</div>
    </article>)}
  </section>;
}
export function ServerMonitor({ apiBaseUrl, active }: Props) {
  const [info, setInfo] = useState<Monitor | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await fetch(apiBaseUrl + '/owner/server-monitor', {
        credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json' }, signal,
      });
      if (!res.ok) throw new Error(res.status === 403 ? 'Owner login required.' : 'Monitor is temporarily unavailable.');
      const value = (await res.json()) as Monitor;
      if (signal?.aborted) return;
      setInfo(value);
      setError('');
    } catch (caught) {
      if (signal?.aborted) return;
      setError(caught instanceof Error ? caught.message : 'Could not refresh monitor.');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [apiBaseUrl]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void refresh(controller.signal);
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh(controller.signal);
    }, 15_000);
    return () => { window.clearInterval(interval); controller.abort(); };
  }, [active, refresh]);
  if (!active) return null;
  const gold = info?.pu_prime.gold ?? info?.vantage.gold;
  return <section className="ssm" aria-label="Super Signals server monitoring">
    <header className="ssm-title">
      <div><p className="ssm-eyebrow">OWNER ONLY · LIVE INFRASTRUCTURE</p><h1>Server Monitor</h1><p>PU Prime, Vantage and gold in one place. Read-only, auto-refreshing every 15 seconds.</p></div>
      <button type="button" className="ssm-refresh" onClick={() => void refresh()}>Refresh now</button>
    </header>
    {loading && !info && <p className="ssm-card">Loading live account snapshots…</p>}
    {error && <p role="status" className="ssm-error">{error}{info ? ' Showing last successful data.' : ''}</p>}
    {info && <>
      <div className="ssm-grid">
        <WorkerCard broker={info.pu_prime} heading="PU Prime · Live" note="COPY TRADING MASTER" />
        <WorkerCard broker={info.vantage} heading="Vantage · Paper" note="SUPER SIGNALS SOURCE" />
      </div>
      <div className="ssm-grid ssm-secondary">
        <div className="ssm-card ssm-price">
          <span className="ssm-eyebrow">GOLD · XAUUSD</span>
          <div className="ssm-metrics"><div><span>Bid</span><strong>{amount(gold?.bid)}</strong></div><div><span>Ask</span><strong>{amount(gold?.ask)}</strong></div></div>
          <p className="ssm-meta">{gold?.symbol || 'XAUUSD'} · Updated {age(gold?.updated_at)}</p>
        </div>
        <div className="ssm-card ssm-status">
          <span className="ssm-eyebrow">PU PRIME AUTOMATIC COPYING</span>
          <strong className={info.mirror.mode === 'execute' ? 'ssm-up' : 'ssm-down'}>{info.mirror.mode.toUpperCase()}</strong>
          <p>Fixed 0.01 lots per TP · Future trades only</p>
          <small>This displays the configured mode, not proof of an executed order.</small>
        </div>
      </div>
      <TradeRows broker={info.pu_prime} heading="PU Prime open trades" />
      <TradeRows broker={info.vantage} heading="Vantage open trades" />
      <div className="ssm-footer">Last checked {age(info.read_at)} · All values come from authenticated MT5 worker snapshots. <a href="https://www.myfxbook.com/settings" target="_blank" rel="noreferrer">Myfxbook settings</a></div>
    </>}
  </section>;
}
