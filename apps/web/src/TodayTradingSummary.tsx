import { useCallback, useEffect, useRef, useState } from 'react';

import './today-trading-summary.css';

type TodaySummary = {
  timezone: string;
  session_started_at: string;
  trades: number;
  wins: number;
  losses: number;
  breakeven: number;
  open: number;
  pending: number;
  settling: number;
  realised_pnl: number;
  winning_pips: number;
  net_pips: number;
  broker_trade_action_created: boolean;
};

type Props = {
  apiBaseUrl: string;
  currency: string;
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

function pips(value: number): string {
  const sign = value > 0 ? '+' : '';
  return `${sign}${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value)}`;
}

function pnlClass(value: number): string {
  if (value === 0) return 'is-flat';
  return value > 0 ? 'is-positive' : 'is-negative';
}

export function TodayTradingSummary({ apiBaseUrl, currency }: Props) {
  const [summary, setSummary] = useState<TodaySummary | null>(null);
  const [stale, setStale] = useState(false);
  const brokerSyncRunning = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      const query = new URLSearchParams({ timezone_name: timezone });
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
      setStale(true);
    }
  }, [apiBaseUrl]);

  const reconcileBroker = useCallback(async () => {
    if (brokerSyncRunning.current) return;
    brokerSyncRunning.current = true;
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/dashboard/performance/sync`, {
        method: 'POST',
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!response.ok) throw new Error('broker_reconciliation_unavailable');
      window.dispatchEvent(new Event('super-signals-ledger-synced'));
      await refresh();
    } catch {
      setStale(true);
    } finally {
      brokerSyncRunning.current = false;
    }
  }, [apiBaseUrl, refresh]);

  useEffect(() => {
    void refresh();
    void reconcileBroker();
    const readInterval = window.setInterval(() => void refresh(), 5000);
    const brokerInterval = window.setInterval(() => void reconcileBroker(), 30000);
    const onFocus = () => {
      void refresh();
      void reconcileBroker();
    };
    const onLedgerSynced = () => void refresh();
    window.addEventListener('focus', onFocus);
    window.addEventListener('super-signals-ledger-synced', onLedgerSynced);
    return () => {
      window.clearInterval(readInterval);
      window.clearInterval(brokerInterval);
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('super-signals-ledger-synced', onLedgerSynced);
    };
  }, [reconcileBroker, refresh]);

  if (!summary) {
    return <section className="today-trading-card today-trading-card--loading" aria-label="Today's trading summary" aria-live="polite">
      <div><span>Today</span><strong>{stale ? 'Updating…' : 'Loading…'}</strong></div>
    </section>;
  }

  return <section className="today-trading-card" aria-label="Today's trading summary" aria-live="polite">
    <div className="today-trading-head">
      <div className="today-trading-primary"><span>Today</span><strong>{summary.trades} trade{summary.trades === 1 ? '' : 's'}</strong></div>
      <div className="today-trading-headline"><span>Pips won</span><strong className={pnlClass(summary.winning_pips)}>{pips(summary.winning_pips)}</strong></div>
      <div className="today-trading-headline"><span>Net pips</span><strong className={pnlClass(summary.net_pips)}>{pips(summary.net_pips)}</strong></div>
      <div className="today-trading-headline"><span>Trading P/L</span><strong className={pnlClass(summary.realised_pnl)}>{money(summary.realised_pnl, currency)}</strong></div>
    </div>
    <div className="today-trading-stats">
      <div><strong>{summary.wins}</strong><span>Wins</span></div>
      <div><strong>{summary.losses}</strong><span>Losses</span></div>
      <div><strong>{summary.breakeven}</strong><span>BE</span></div>
      <div><strong>{summary.open}</strong><span>Open</span></div>
      <div><strong>{summary.pending}</strong><span>Pending</span></div>
      {summary.settling > 0 && <div><strong>{summary.settling}</strong><span>Settling</span></div>}
    </div>
    <small>
      {stale
        ? 'Broker reconciliation updating — last confirmed values shown'
        : 'Auto-updates from broker-backed trade records'}
    </small>
  </section>;
}
