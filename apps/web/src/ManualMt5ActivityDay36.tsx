import { useCallback, useEffect, useMemo, useState } from 'react';

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

type AccountAccess = { role: string };

type OwnerOpenPosition = {
  position_id: string;
  broker_position_id: string;
  signal_id: string;
  tp_index: number;
  symbol: string;
  side: string;
  volume: number;
  current_price: number | null;
  take_profit: number | null;
};

type DashboardResponse = {
  connection: { account_environment: string | null };
  open_positions: OwnerOpenPosition[];
};

type CloseResponse = {
  requested_count: number;
  closed_count: number;
  already_closed_count: number;
  failed_count: number;
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

function price(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 5 }).format(value);
}

function apiError(body: unknown, fallback: string): string {
  if (typeof body !== 'object' || body === null || !('detail' in body)) return fallback;
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === 'object' && detail !== null && 'message' in detail) {
    return String((detail as { message: unknown }).message);
  }
  return fallback;
}

export function ManualMt5ActivityDay36({ apiBaseUrl }: Props) {
  const [data, setData] = useState<ManualActivityResponse | null>(null);
  const [isOwner, setIsOwner] = useState(false);
  const [accountEnvironment, setAccountEnvironment] = useState<string | null>(null);
  const [positions, setPositions] = useState<OwnerOpenPosition[]>([]);
  const [closingKey, setClosingKey] = useState<string | null>(null);
  const [closeNotice, setCloseNotice] = useState<string | null>(null);
  const [closeError, setCloseError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [activityResponse, accessResponse] = await Promise.all([
        fetch(`${apiBaseUrl}/account/mt5/manual-actions`, {
          credentials: 'include',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        }),
        fetch(`${apiBaseUrl}/auth/me`, {
          credentials: 'include',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        }),
      ]);

      if (activityResponse.ok) {
        const next = (await activityResponse.json()) as ManualActivityResponse;
        if (!next.broker_trade_action_created) setData(next);
      }

      if (!accessResponse.ok) {
        setIsOwner(false);
        setPositions([]);
        return;
      }
      const access = (await accessResponse.json()) as AccountAccess;
      const owner = access.role === 'owner';
      setIsOwner(owner);
      if (!owner) {
        setPositions([]);
        return;
      }

      const dashboardResponse = await fetch(`${apiBaseUrl}/account/mt5/dashboard`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!dashboardResponse.ok) return;
      const dashboard = (await dashboardResponse.json()) as DashboardResponse;
      setAccountEnvironment(dashboard.connection.account_environment);
      setPositions(Array.isArray(dashboard.open_positions) ? dashboard.open_positions : []);
    } catch {
      // These controls and outside-app reconciliation must never hide or break the
      // main account dashboard if an auxiliary broker read is temporarily unavailable.
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 30000);
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

  const trades = useMemo(() => {
    const grouped = new Map<string, OwnerOpenPosition[]>();
    for (const position of positions) {
      const existing = grouped.get(position.signal_id) ?? [];
      existing.push(position);
      grouped.set(position.signal_id, existing);
    }
    return Array.from(grouped.entries());
  }, [positions]);

  const closeAtMarket = useCallback(async (
    endpoint: string,
    key: string,
    confirmation: string,
  ) => {
    if (!window.confirm(confirmation)) return;
    setClosingKey(key);
    setCloseNotice(null);
    setCloseError(null);
    try {
      const response = await fetch(`${apiBaseUrl}${endpoint}`, {
        method: 'POST',
        credentials: 'include',
        headers: { Accept: 'application/json' },
      });
      const body = (await response.json()) as CloseResponse | unknown;
      if (!response.ok) throw new Error(apiError(body, 'The broker did not confirm the close.'));
      const result = body as CloseResponse;
      if (result.failed_count > 0) {
        setCloseNotice(`${result.closed_count} position${result.closed_count === 1 ? '' : 's'} closed at market. ${result.failed_count} could not be confirmed and remain untouched.`);
      } else if (result.closed_count > 0) {
        setCloseNotice(`${result.closed_count} position${result.closed_count === 1 ? '' : 's'} closed at the broker's market price.`);
      } else {
        setCloseNotice('The broker reports that position is already closed.');
      }
      window.dispatchEvent(new Event('super-signals-ledger-synced'));
      await refresh();
    } catch (caught) {
      setCloseError(caught instanceof Error ? caught.message : 'The broker did not confirm the close.');
      await refresh();
    } finally {
      setClosingKey(null);
    }
  }, [apiBaseUrl, refresh]);

  if (!data && !isOwner) return null;

  return <>
    {isOwner && <section className="day32-section" aria-labelledby="owner-manual-close-title">
      <div className="day32-section-head">
        <div><span>Owner Admin · {accountEnvironment === 'demo' ? 'Demo only' : 'Protected'}</span><h2 id="owner-manual-close-title">Manual close at current price</h2></div>
      </div>
      <p>Close a mapped Super Signals position directly at the broker's current executable market price. This is an Owner action, not a provider instruction.</p>
      {closeNotice && <div className="day32-inline-warning" role="status"><span>Manual close completed</span><strong>{closeNotice}</strong></div>}
      {closeError && <div className="day32-inline-warning" role="alert"><span>Manual close not confirmed</span><strong>{closeError}</strong></div>}
      {positions.length === 0
        ? <div className="day32-empty day32-empty--compact"><strong>No open Super Signals positions to close</strong><span>The control will appear here as soon as a broker-mapped position is open.</span></div>
        : <div className="day32-position-list">{trades.map(([signalId, tradePositions]) => {
            const first = tradePositions[0];
            const tradeKey = `trade-${signalId}`;
            return <article className="day32-position-card" key={signalId}>
              <div className="day32-position-title"><div><span className={`day32-side day32-side--${first.side.toLowerCase()}`}>{first.side}</span><strong>{first.symbol}</strong><small>{tradePositions.length} open position{tradePositions.length === 1 ? '' : 's'}</small></div></div>
              <div className="owner-close-position-stack">
                {tradePositions.map((position) => {
                  const positionKey = `position-${position.position_id}`;
                  return <div className="owner-close-position-row" key={position.position_id}>
                    <div><strong>TP{position.tp_index}</strong><span>{position.volume} lots · now {price(position.current_price)} · TP {price(position.take_profit)}</span></div>
                    <button className="button button--quiet" type="button" disabled={closingKey !== null} onClick={() => void closeAtMarket(
                      `/account/mt5/owner-close-position/${position.position_id}`,
                      positionKey,
                      `Close ${position.symbol} TP${position.tp_index} now at the broker's current market price?`,
                    )}>{closingKey === positionKey ? 'Closing…' : 'Close position now'}</button>
                  </div>;
                })}
              </div>
              <button className="button" type="button" disabled={closingKey !== null} onClick={() => void closeAtMarket(
                `/account/mt5/owner-close-trade/${signalId}`,
                tradeKey,
                `Close all ${tradePositions.length} open ${first.symbol} positions in this Super Signals trade now at the broker's current market price?`,
              )}>{closingKey === tradeKey ? 'Closing trade…' : 'Close entire trade now'}</button>
            </article>;
          })}</div>}
    </section>}

    {data && <section className="day32-section" aria-labelledby="manual-mt5-title">
      <div className="day32-section-head">
        <div><span>Vantage MT5</span><h2 id="manual-mt5-title">Changes made directly in MT5</h2></div>
      </div>
      {data.actions.length === 0
        ? <div className="day32-empty day32-empty--compact"><strong>No outside-app changes detected</strong><span>If you change a Super Signals position directly in MT5, it will be reflected here without Super Signals reversing it.</span></div>
        : <ol className="day32-activity-list">{data.actions.map((item) => <li key={item.audit_id}>
            <i className="day32-activity-dot day32-activity-dot--neutral" />
            <div><strong>{item.label}</strong><span>{item.detail}</span><small>{shortTime(item.occurred_at)}</small></div>
          </li>)}</ol>}
    </section>}
  </>;
}
