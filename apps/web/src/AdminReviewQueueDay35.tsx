import { useCallback, useEffect, useMemo, useState } from 'react';

import './admin-review-queue-day35.css';

type ReviewItem = {
  id: string;
  message_id: string;
  source_id: string;
  source_title: string;
  telegram_message_id: number;
  revision_index: number;
  review_stage: string;
  review_status: string;
  reason: string;
  matched_rules: string[];
  raw_text: string;
  classification: string | null;
  decision_status: string | null;
  classifier_version: string | null;
  parse_status: string | null;
  parser_version: string | null;
  validator_version: string | null;
  symbol: string | null;
  direction: string | null;
  entry_price: number | null;
  stop_loss: number | null;
  take_profits: string[];
  size_multiplier: number | null;
  queued_at: string;
};

type Props = {
  apiBaseUrl: string;
  onOpenSources: () => void;
};

type StageFilter = 'all' | 'classification' | 'parser' | 'validation';

function when(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Unknown time';
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function compact(value: string, max = 220): string {
  const normalized = value.trim().replace(/\s+/g, ' ');
  return normalized.length > max ? `${normalized.slice(0, max - 1)}…` : normalized;
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail = typeof body === 'object' && body !== null && 'detail' in body
      ? (body as { detail: unknown }).detail
      : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail
      ? String((detail as { message: unknown }).message)
      : 'The review queue is temporarily unavailable.';
    throw new Error(message);
  }
  return body;
}

export function AdminReviewQueueDay35({ apiBaseUrl, onOpenSources }: Props) {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stage, setStage] = useState<StageFilter>('all');
  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/telegram/reviews/recent?limit=100`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      setItems(await readJson<ReviewItem[]>(response));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The review queue is temporarily unavailable.');
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => { void load(); }, [load]);

  const counts = useMemo(() => ({
    all: items.length,
    classification: items.filter((item) => item.review_stage === 'classification').length,
    parser: items.filter((item) => item.review_stage === 'parser').length,
    validation: items.filter((item) => item.review_stage === 'validation').length,
  }), [items]);

  const visible = useMemo(() => {
    const query = search.trim().toLowerCase();
    return items.filter((item) => {
      if (stage !== 'all' && item.review_stage !== stage) return false;
      if (!query) return true;
      return [
        item.source_title,
        item.raw_text,
        item.reason,
        item.symbol ?? '',
        item.direction ?? '',
        item.classification ?? '',
      ].some((value) => value.toLowerCase().includes(query));
    });
  }, [items, search, stage]);

  return <section className="day35-review-queue" aria-labelledby="day35-review-title">
    <div className="workspace-page-header">
      <div>
        <p className="eyebrow">Day 35 · Admin review queue</p>
        <h1 id="day35-review-title">Messages that need a human look</h1>
        <p className="intro">See exactly what the classifier, parser or validator could not safely settle. This screen is review-only and cannot create a trade.</p>
      </div>
      <span className="workspace-role-pill">FAIL CLOSED</span>
    </div>

    <div className="day35-review-toolbar">
      <div className="day35-review-stage-tabs" aria-label="Review stage">
        {(['all', 'classification', 'parser', 'validation'] as StageFilter[]).map((value) => <button key={value} type="button" className={stage === value ? 'is-selected' : ''} onClick={() => setStage(value)}><span>{value === 'all' ? 'All' : value}</span><b>{counts[value]}</b></button>)}
      </div>
      <div className="day35-review-search"><input aria-label="Search review queue" placeholder="Search source, symbol or text" value={search} onChange={(event) => setSearch(event.target.value)} /><button type="button" onClick={() => void load()} disabled={loading}>{loading ? 'Refreshing…' : 'Refresh'}</button></div>
    </div>

    {error && <div className="day35-review-error" role="alert">{error}</div>}

    <div className="day35-review-context"><div><strong>{visible.length}</strong><span>shown from the latest {items.length} loaded</span></div><button type="button" onClick={onOpenSources}>Open signal sources</button></div>

    {loading && !items.length ? <div className="day35-review-loading"><div /><div /><div /></div> : visible.length ? <div className="day35-review-list">
      {visible.map((item) => {
        const isOpen = expanded === item.id;
        return <article className={`day35-review-card day35-review-card--${item.review_stage}`} key={item.id}>
          <button type="button" className="day35-review-card-main" aria-expanded={isOpen} onClick={() => setExpanded(isOpen ? null : item.id)}>
            <span className="day35-review-stage-dot" aria-hidden="true" />
            <div className="day35-review-card-copy">
              <div className="day35-review-card-heading"><strong>{item.source_title}</strong><span>{item.review_stage}</span><small>{when(item.queued_at)}</small></div>
              <p>{compact(item.raw_text || item.reason)}</p>
              <div className="day35-review-tags">
                {item.symbol && <span>{item.symbol}</span>}
                {item.direction && <span>{item.direction}</span>}
                {item.classification && <span>{item.classification}</span>}
                <span className="day35-review-reason">{compact(item.reason, 90)}</span>
              </div>
            </div>
            <span className="day35-review-chevron" aria-hidden="true">{isOpen ? '−' : '+'}</span>
          </button>

          {isOpen && <div className="day35-review-detail">
            <section><span>Original message</span><p>{item.raw_text || 'No source text stored.'}</p></section>
            <div className="day35-review-detail-grid">
              <span><small>Review status</small><strong>{item.review_status}</strong></span>
              <span><small>Decision</small><strong>{item.decision_status ?? '—'}</strong></span>
              <span><small>Parse status</small><strong>{item.parse_status ?? '—'}</strong></span>
              <span><small>Revision</small><strong>{item.revision_index}</strong></span>
              <span><small>Entry</small><strong>{item.entry_price ?? '—'}</strong></span>
              <span><small>Stop loss</small><strong>{item.stop_loss ?? '—'}</strong></span>
              <span><small>Take profits</small><strong>{item.take_profits.length ? item.take_profits.join(' · ') : '—'}</strong></span>
              <span><small>Size multiplier</small><strong>{item.size_multiplier ?? '—'}</strong></span>
            </div>
            <section><span>Why it was held</span><p>{item.reason}</p>{item.matched_rules.length > 0 && <div className="day35-review-rules">{item.matched_rules.map((rule) => <code key={rule}>{rule}</code>)}</div>}</section>
            <div className="day35-review-version-row"><span>Classifier {item.classifier_version ?? '—'}</span><span>Parser {item.parser_version ?? '—'}</span><span>Validator {item.validator_version ?? '—'}</span><span>Telegram #{item.telegram_message_id}</span></div>
          </div>}
        </article>;
      })}
    </div> : <div className="day35-review-empty"><strong>No items match this view</strong><span>Change the stage or search, or refresh the queue.</span></div>}

    <div className="day35-review-safety"><span aria-hidden="true">◎</span><div><strong>Review does not equal execution.</strong><small>These are held because the deterministic pipeline could not safely continue. Viewing them here does not approve, publish or place anything at the broker.</small></div></div>
  </section>;
}
