import { useEffect, useState } from 'react';

import './gold-price-strip.css';

type GoldQuote = {
  symbol: string;
  price: number | null;
  bid: number | null;
  ask: number | null;
  quote_time: string | null;
  read_at: string;
  available: boolean;
  stale: boolean;
  source: string;
};

type Props = {
  apiBaseUrl: string;
};

const LIVE_REFRESH_MS = 2000;
const DELAYED_REFRESH_MS = 15000;
const HIDDEN_REFRESH_MS = 30000;

function goldPrice(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function compactPrice(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function GoldPriceStrip({ apiBaseUrl }: Props) {
  const [quote, setQuote] = useState<GoldQuote | null>(null);
  const [feedError, setFeedError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    let controller: AbortController | null = null;

    const schedule = (delay: number) => {
      if (cancelled) return;
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => void poll(), delay);
    };

    const poll = async () => {
      if (cancelled) return;
      if (document.hidden) {
        schedule(HIDDEN_REFRESH_MS);
        return;
      }

      controller?.abort();
      controller = new AbortController();
      try {
        const response = await fetch(`${apiBaseUrl}/account/mt5/dashboard/gold-quote`, {
          credentials: 'include',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
          signal: controller.signal,
        });
        if (!response.ok) throw new Error('gold_quote_unavailable');
        const next = (await response.json()) as GoldQuote;
        if (cancelled) return;
        setQuote(next);
        setFeedError(false);
        schedule(next.available && !next.stale ? LIVE_REFRESH_MS : DELAYED_REFRESH_MS);
      } catch (error) {
        if (cancelled || (error instanceof DOMException && error.name === 'AbortError')) return;
        setFeedError(true);
        schedule(DELAYED_REFRESH_MS);
      }
    };

    const onVisibilityChange = () => {
      if (!document.hidden) {
        if (timer !== null) window.clearTimeout(timer);
        void poll();
      }
    };

    void poll();
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      controller?.abort();
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [apiBaseUrl]);

  const live = Boolean(quote?.available && !quote.stale && !feedError);
  const delayed = Boolean(quote?.price !== null && quote?.price !== undefined && !live);
  const stateLabel = live ? 'Live' : delayed ? 'Delayed' : quote === null && !feedError ? 'Connecting' : 'Unavailable';

  return <section className={`gold-price-strip ${live ? 'gold-price-strip--live' : ''}`} aria-label="Live gold price">
    <div className="gold-price-strip__identity">
      <span className="gold-price-strip__mark" aria-hidden="true" />
      <div><strong>GOLD</strong><small>XAU / USD</small></div>
    </div>
    <div className="gold-price-strip__price">
      <strong>{goldPrice(quote?.price ?? null)}</strong>
      <span className={`gold-price-strip__state gold-price-strip__state--${live ? 'live' : delayed ? 'delayed' : 'offline'}`}><i />{stateLabel}</span>
    </div>
    <div className="gold-price-strip__spread" aria-label="Gold bid and ask">
      <span>Bid <strong>{compactPrice(quote?.bid ?? null)}</strong></span>
      <span>Ask <strong>{compactPrice(quote?.ask ?? null)}</strong></span>
    </div>
  </section>;
}
