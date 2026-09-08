import { useCallback, useEffect, useState } from 'react';

type ManualAction = {
  audit_id: number | string;
  position_id: string;
  action_type: string;
  label: string;
  detail: string;
  old_value: string | null;
  new_value: string | null;
  trade_reference: string | null;
  occurred_at: string;
};

type ManualActivityResponse = {
  stop_loss_changes: number;
  take_profit_changes: number;
  manual_closes: number;
  actions: ManualAction[];
  broker_trade_action_created: boolean;
};

type Props = { apiBaseUrl: string };

function shortTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '—';
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(parsed);
}

export function ManualMt5ActivityDay36({ apiBaseUrl }: Props) {
  const [data, setData] = useState<ManualActivityResponse | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${apiBaseUrl}/account/mt5/manual-actions`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!response.ok) return;
      const next = (await response.json()) as ManualActivityResponse;
      if (next.broker_trade_action_created) return;
      setData(next);
    } catch {
      // Outside-app reconciliation is observability only. A transient broker read
      // must never hide or break the main account dashboard.
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    void refresh();
    // This endpoint performs broker-authoritative reconciliation. It is observability
    // only, so do not poll MetaAPI every 30 seconds and compete with signal execution.
    // Five minutes plus focus/ledger events is sufficient for manual-action display.
    const interval = window.setInterval(() => void refresh(), 300000);
    const onFocus = () => void refresh();
    const onLedgerSynced = () => void refresh();
    window.addEventListener('focus', onFocus);
    window.addEventListener('super-signals-ledger-synced', onLedgerSynced);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('super-signals-ledger-synced', onLedgerSynced);
    };
  }, [refresh]);

  if (!data) return null;

  return <section className="day32-section" aria-labelledby="manual-mt5-title">
    <div className="day32-section-head">
      <div><span>Vantage MT5</span><h2 id="manual-mt5-title">Changes made directly in MT5</h2></div>
    </div>
    {data.actions.length === 0
      ? <div className="day32-empty day32-empty--compact"><strong>No outside-app changes detected</strong><span>If you change a Smart Signals position directly in MT5, it will be reflected here without Smart Signals reversing it.</span></div>
      : <ol className="day32-activity-list">{data.actions.map((item) => <li key={item.audit_id}>
          <i className="day32-activity-dot day32-activity-dot--neutral" />
          <div><strong>{item.label}</strong><span>{item.detail}</span><small>{shortTime(item.occurred_at)}</small></div>
        </li>)}</ol>}
  </section>;
}
