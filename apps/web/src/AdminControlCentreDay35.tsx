import { useCallback, useEffect, useMemo, useState } from 'react';

import { AdminMemberControlsDay35 } from './AdminMemberControlsDay35';
import { AdminOperationsDay35 } from './AdminOperationsDay35';
import './admin-control-centre-day35.css';

type Tone = 'healthy' | 'attention' | 'critical' | 'neutral';
type SafetyPanel = 'none' | 'members';

type ReviewStage = {
  stage: string;
  open_count: number;
  oldest_at: string | null;
  newest_at: string | null;
};

type AttentionItem = {
  key: string;
  tone: Tone;
  title: string;
  detail: string;
  count: number;
  area: string;
};

type RecentEvent = {
  event_type: string;
  entity_type: string;
  created_at: string;
};

type ControlCentreResponse = {
  generated_at: string;
  overall_status: Tone;
  open_signals: number;
  pending_signals: number;
  trading_active_users: number;
  trading_stopped_users: number;
  source_live: number;
  source_testing: number;
  source_paused: number;
  source_revoked: number;
  telegram_connected: number;
  telegram_attention: number;
  mt5_connected: number;
  mt5_attention: number;
  active_users: number;
  invited_users: number;
  suspended_users: number;
  revoked_users: number;
  review_open: number;
  review_open_24h: number;
  message_errors_24h: number;
  publication_failed: number;
  publication_pending: number;
  push_failed_24h: number;
  telegram_notification_failed_24h: number;
  live_board_ready: boolean;
  live_board_pinned: boolean;
  review_stages: ReviewStage[];
  attention: AttentionItem[];
  recent_events: RecentEvent[];
  broker_trade_action_created: boolean;
};

type Props = {
  apiBaseUrl: string;
  displayName: string;
  roleLabel: string;
  onOpenPortfolio: () => void;
  onOpenSources: () => void;
  onOpenSettings: () => void;
};

function snapshotTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'just now';
  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date);
}

function dateTime(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function eventLabel(value: string): string {
  return value
    .replace(/^mt5\./, '')
    .replace(/^admin\./, '')
    .replace(/^trading\./, '')
    .replace(/^source\./, '')
    .replaceAll('_', ' ')
    .replaceAll('.', ' · ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body
      ? (body as { detail: unknown }).detail
      : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : 'Control Centre is temporarily unavailable.';
    throw new Error(message);
  }
  return body;
}

export function AdminControlCentreDay35({
  apiBaseUrl,
  displayName,
  roleLabel,
  onOpenPortfolio,
  onOpenSources,
  onOpenSettings,
}: Props) {
  const [data, setData] = useState<ControlCentreResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [safetyPanel, setSafetyPanel] = useState<SafetyPanel>('none');
  const [operationsOpen, setOperationsOpen] = useState(false);
  const ownerView = roleLabel.toLowerCase().includes('owner');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/access/day35/control-centre`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      setData(await readJson<ControlCentreResponse>(response));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Control Centre is temporarily unavailable.');
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => { void load(); }, [load]);

  const deliveryFailures = useMemo(() => {
    if (!data) return 0;
    return data.publication_failed + data.push_failed_24h + data.telegram_notification_failed_24h;
  }, [data]);

  if (loading && !data) {
    return <section className="day35-control-centre" aria-label="Loading Control Centre">
      <div className="day35-control-skeleton day35-control-skeleton--hero" />
      <div className="day35-control-skeleton-grid"><div /><div /><div /><div /></div>
    </section>;
  }

  return <section className="day35-control-centre" aria-labelledby="day35-control-title">
    <div className="day35-control-hero">
      <div>
        <p className="eyebrow">Day 35 · Admin Control Centre</p>
        <h1 id="day35-control-title">Good morning, {displayName}.</h1>
        <p>One operating view for the signal network, broker state, review workload, delivery health and users.</p>
      </div>
      <div className={`day35-control-status day35-control-status--${data?.overall_status ?? 'neutral'}`}>
        <span aria-hidden="true" />
        <div><small>{roleLabel}</small><strong>{data?.overall_status === 'healthy' ? 'System healthy' : data?.overall_status === 'critical' ? 'Action required' : 'Attention needed'}</strong></div>
      </div>
    </div>

    <div className="day35-control-snapshot">
      <span>{data ? `Snapshot ${snapshotTime(data.generated_at)}` : 'Snapshot unavailable'}</span>
      <button type="button" onClick={() => void load()} disabled={loading}>{loading ? 'Refreshing…' : 'Refresh'}</button>
    </div>

    {error && <div className="day35-control-error" role="alert">{error}</div>}

    {data && <>
      <section className="day35-attention" aria-labelledby="day35-attention-title">
        <div className="day35-section-heading"><div><p className="eyebrow">Operator attention</p><h2 id="day35-attention-title">What needs looking at?</h2></div><span>{data.attention.filter((item) => item.tone !== 'healthy' && item.tone !== 'neutral').length} priority</span></div>
        <div className="day35-attention-list">
          {data.attention.map((item) => <article className={`day35-attention-item day35-attention-item--${item.tone}`} key={item.key}>
            <span className="day35-attention-indicator" aria-hidden="true" />
            <div><strong>{item.title}</strong><small>{item.detail}</small></div>
            {item.count > 0 && <b>{item.count}</b>}
          </article>)}
        </div>
      </section>

      <div className="day35-control-grid" aria-label="Operational status">
        <article className="day35-control-card day35-control-card--trading">
          <div className="day35-control-card-head"><span>Trading now</span><b>{data.open_signals || data.pending_signals ? 'LIVE STATE' : 'CLEAR'}</b></div>
          <strong className="day35-control-big">{data.open_signals}</strong><small>open canonical trade{data.open_signals === 1 ? '' : 's'}</small>
          <div className="day35-control-mini"><span><b>{data.pending_signals}</b> pending</span><span><b>{data.trading_active_users}</b> automation active</span></div>
        </article>

        <article className="day35-control-card">
          <div className="day35-control-card-head"><span>Signal network</span><b>{data.source_paused ? 'CHECK' : 'READY'}</b></div>
          <strong className="day35-control-big">{data.source_live}</strong><small>sources in Live</small>
          <div className="day35-control-mini"><span><b>{data.source_testing}</b> testing</span><span><b>{data.source_paused}</b> paused</span></div>
          <button type="button" onClick={onOpenSources}>Open signal sources</button>
        </article>

        <article className="day35-control-card day35-control-card--review">
          <div className="day35-control-card-head"><span>Review queue</span><b>{data.review_open ? 'OPEN' : 'CLEAR'}</b></div>
          <strong className="day35-control-big">{data.review_open}</strong><small>items awaiting review</small>
          <div className="day35-control-mini"><span><b>{data.review_open_24h}</b> added 24h</span><span><b>{data.review_stages.length}</b> stages</span></div>
        </article>

        <article className="day35-control-card">
          <div className="day35-control-card-head"><span>Connections</span><b>{data.telegram_attention || data.mt5_attention ? 'CHECK' : 'READY'}</b></div>
          <strong className="day35-control-big">{data.telegram_connected + data.mt5_connected}</strong><small>connected services</small>
          <div className="day35-control-mini"><span><b>{data.telegram_connected}</b> Telegram readers</span><span><b>{data.mt5_connected}</b> MT5</span></div>
        </article>

        <article className="day35-control-card">
          <div className="day35-control-card-head"><span>Member delivery</span><b>{deliveryFailures ? 'CHECK' : 'CLEAR'}</b></div>
          <strong className="day35-control-big">{deliveryFailures}</strong><small>failed deliveries</small>
          <div className="day35-control-mini"><span><b>{data.publication_pending}</b> Telegram pending</span><span><b>{data.message_errors_24h}</b> intake errors 24h</span></div>
        </article>

        <article className="day35-control-card">
          <div className="day35-control-card-head"><span>Members</span><b>ACCESS</b></div>
          <strong className="day35-control-big">{data.active_users}</strong><small>active platform users</small>
          <div className="day35-control-mini"><span><b>{data.invited_users}</b> invited</span><span><b>{data.suspended_users}</b> suspended</span><span><b>{data.revoked_users}</b> revoked</span></div>
        </article>
      </div>

      <div className="day35-control-columns">
        <section className="day35-review-breakdown" aria-labelledby="day35-review-breakdown-title">
          <div className="day35-section-heading"><div><p className="eyebrow">Review workload</p><h2 id="day35-review-breakdown-title">Open queue by stage</h2></div></div>
          {data.review_stages.length ? <div className="day35-review-stages">{data.review_stages.map((stage) => <article key={stage.stage}><div><strong>{stage.stage}</strong><small>Oldest {dateTime(stage.oldest_at)}</small></div><b>{stage.open_count}</b></article>)}</div> : <div className="day35-control-empty">No review items are open.</div>}
        </section>

        <section className="day35-system-checks" aria-labelledby="day35-system-checks-title">
          <div className="day35-section-heading"><div><p className="eyebrow">Shared member layer</p><h2 id="day35-system-checks-title">Live board & delivery</h2></div></div>
          <div className="day35-check-row"><span className={data.live_board_ready ? 'is-good' : 'is-bad'} /><div><strong>Live Trades Board</strong><small>{data.live_board_ready ? 'Ready' : 'Not ready'}</small></div></div>
          <div className="day35-check-row"><span className={data.live_board_pinned ? 'is-good' : 'is-bad'} /><div><strong>Telegram pin</strong><small>{data.live_board_pinned ? 'Permanent board pinned' : 'Pin missing'}</small></div></div>
          <div className="day35-check-row"><span className={data.publication_failed === 0 ? 'is-good' : 'is-bad'} /><div><strong>Trade publication</strong><small>{data.publication_failed === 0 ? 'No failed posts' : `${data.publication_failed} failed post${data.publication_failed === 1 ? '' : 's'}`}</small></div></div>
        </section>
      </div>

      <section className="day35-quick-actions" aria-labelledby="day35-quick-actions-title">
        <div className="day35-section-heading"><div><p className="eyebrow">Operator shortcuts</p><h2 id="day35-quick-actions-title">Go straight to the work</h2></div></div>
        <div><button type="button" onClick={() => setOperationsOpen(!operationsOpen)}><span>◎</span><strong>Trades &amp; failures</strong><small>Canonical trade and issue drill-down</small></button><button type="button" onClick={onOpenPortfolio}><span>◈</span><strong>Signal Portfolio</strong><small>Rank provider performance</small></button><button type="button" onClick={onOpenSources}><span>⌁</span><strong>Signal sources</strong><small>Live, Testing and Paused</small></button><button type="button" onClick={onOpenSettings}><span>⚙</span><strong>Admin settings</strong><small>Connections and access</small></button></div>
      </section>
      {operationsOpen && <div className="day35-operations-inline"><AdminOperationsDay35 apiBaseUrl={apiBaseUrl} /></div>}

      {ownerView && <section className="day35-danger-zone" aria-labelledby="day35-danger-zone-title">
        <div className="day35-section-heading"><div><p className="eyebrow">Safety &amp; access</p><h2 id="day35-danger-zone-title">Confirmed member controls</h2></div><span>Owner only</span></div>
        <p className="day35-danger-zone-copy">Opening member controls does nothing by itself. Revoking a member requires the exact typed phrase, stops that member's automation first, closes only mapped Super Signals positions, and records immutable Owner audit evidence.</p>
        <div className="day35-danger-zone-actions">
          <button type="button" className={safetyPanel === 'members' ? 'is-open' : ''} onClick={() => setSafetyPanel(safetyPanel === 'members' ? 'none' : 'members')}><span>Member access</span><strong>Review users &amp; revoke</strong><small>Owner only · mapped positions only</small></button>
        </div>
        {safetyPanel === 'members' && <div className="day35-danger-zone-panel"><AdminMemberControlsDay35 apiBaseUrl={apiBaseUrl} /></div>}
      </section>}

      <section className="day35-recent-events" aria-labelledby="day35-recent-events-title">
        <div className="day35-section-heading"><div><p className="eyebrow">Immutable audit trail</p><h2 id="day35-recent-events-title">Recent operator events</h2></div></div>
        {data.recent_events.length ? <div>{data.recent_events.map((event, index) => <article key={`${event.created_at}:${event.event_type}:${index}`}><span /><div><strong>{eventLabel(event.event_type)}</strong><small>{event.entity_type} · {dateTime(event.created_at)}</small></div></article>)}</div> : <div className="day35-control-empty">No recent operator events.</div>}
      </section>

      <div className="day35-control-safety"><span aria-hidden="true">◎</span><div><strong>Normal Control Centre views are observability only</strong><small>Owner member revoke is isolated above, requires explicit typed confirmation, and reuses the mapped-only Day 31 Stop &amp; Close gateway. There is no global emergency-stop control.</small></div></div>
    </>}
  </section>;
}
