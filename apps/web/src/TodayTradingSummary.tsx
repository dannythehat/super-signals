import { useCallback, useEffect, useState } from 'react';

import './today-trading-summary.css';

type TodaySummary = {
  timezone: string;
  session_started_at: string;
  trades: number;
  wins: number;
  losses: number;
  breakeven: number;
  open: number;
  pending: number | null;
  settling: number;
  realised_pnl: number;
  broker_trade_action_created: boolean;
};

type Props = {
  apiBaseUrl: string;
  currency: string;
  timezoneName: string;
  active?: boolean;
};

function money(value: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: currency || 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
      signDisplay: 'exceptZero',
    }).format(value);
  } catch {
    const sign = value > 0 ? '+' : '';
    return `${sign}${currency || '$'} ${value.toFixed(2)}`;
  }
}

function percent(value: number): string {
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value)}%`;
}

function pnlClass(value: number): string {
  if (value === 0) return 'is-flat';
  return value > 0 ? 'is-positive' : 'is-negative';
}

export function TodayTradingSummary({ apiBaseUrl, currency, timezoneName, active = true }: Props) {
  const [summary, setSummary] = useState<TodaySummary | null>(null);
  const [stale, setStale] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const query = new URLSearchParams({ timezone_name: timezoneName || 'UTC' });
      const response = await fetch(`${apiBaseUrl}/account/mt5/dashboard/today?${query.toString()}`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!response.ok) throw new Error('today_summary_unavailable');
      const next = (await response.json()) as TodaySummary;
      setSummary(next);
      setStale(false);
    } catch {
      // Never clear an already-confirmed summary because one refresh failed. The rest
      // of the app remains fully usable and this card quietly retries later.
      setStale(true);
    }
  }, [apiBaseUrl, timezoneName]);

  useEffect(() => {
    if (!active) return;
    void refresh();
    const readInterval = window.setInterval(() => void refresh(), 60_000);
    const onFocus = () => void refresh();
    const onVisibility = () => { if (document.visibilityState === 'visible') void refresh(); };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(readInterval);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [active, refresh]);

  if (!summary) {
    return <section className="today-trading-card today-trading-card--loading" aria-label="Today's trading summary" aria-live="polite">
      <div><span>Today</span><strong>{stale ? 'Live summary temporarily unavailable' : 'Loading today’s trades…'}</strong></div>
    </section>;
  }

  const decidedTrades = summary.wins + summary.losses;
  const visibleTrades = summary.wins + summary.losses + summary.breakeven + summary.open;
  const winRate = decidedTrades > 0 ? (summary.wins / decidedTrades) * 100 : null;

  return <section className="today-trading-card" aria-label="Today's trading summary" aria-live="polite">
    <div className="today-trading-head">
      <div className="today-trading-primary"><span>Today</span><strong>{visibleTrades} trade{visibleTrades === 1 ? '' : 's'}</strong></div>
      <div className="today-trading-headline"><span>Realised P/L</span><strong className={pnlClass(summary.realised_pnl)}>{money(summary.realised_pnl, currency)}</strong></div>
      <div className="today-trading-headline"><span>Win rate</span><strong>{winRate === null ? '—' : percent(winRate)}</strong></div>
    </div>
    <div className="today-trading-stats">
      <div><strong>{summary.wins}</strong><span>Wins</span></div>
      <div><strong>{summary.losses}</strong><span>Losses</span></div>
      <div><strong>{summary.breakeven}</strong><span>Break-even</span></div>
      <div><strong>{summary.open}</strong><span>Open</span></div>
      <div><strong>{summary.pending === null ? '—' : summary.pending}</strong><span>Pending</span></div>
      {summary.settling > 0 && <div><strong>{summary.settling}</strong><span>Settling</span></div>}
    </div>
    <small>
      {stale
        ? 'Last confirmed values shown · live refresh will retry automatically'
        : `Trading day uses your local timezone · ${summary.timezone}`}
    </small>
  </section>;
}
