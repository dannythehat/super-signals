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

type GoldApiPayload = {
  price?: number | string | null;
  updatedAt?: string | null;
  updated_at?: string | null;
};

type Props = {
  apiBaseUrl: string;
};

const GOLD_API_URL = 'https://api.gold-api.com/price/XAU';
const LIVE_REFRESH_MS = 2000;
const ERROR_REFRESH_MS = 10000;
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

function parsePrice(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  }
  return null;
}

export function GoldPriceStrip({ apiBaseUrl }: Props) {
  void apiBaseUrl;
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
        const response = await fetch(`${GOLD_API_URL}?_=${Date.now()}`, {
          headers: {
            Accept: 'application/json',
            'Cache-Control': 'no-cache',
          },
          cache: 'no-store',
          signal: controller.signal,
        });
        if (!response.ok) throw new Error('gold_quote_unavailable');

        const payload = (await response.json()) as GoldApiPayload;
        const price = parsePrice(payload.price);
        if (price === null) throw new Error('gold_quote_invalid');

        if (cancelled) return;
        const now = new Date().toISOString();
        setQuote({
          symbol: 'XAUUSD',
          price,
          bid: null,
          ask: null,
          quote_time: payload.updatedAt ?? payload.updated_at ?? now,
          read_at: now,
          available: true,
          stale: false,
          source: 'Gold API',
        });
        setFeedError(false);
        schedule(LIVE_REFRESH_MS);
      } catch (error) {
        if (cancelled || (error instanceof DOMException && error.name === 'AbortError')) return;
        setFeedError(true);
        schedule(ERROR_REFRESH_MS);
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
  }, []);

  const live = Boolean(quote?.available && !feedError);
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
    <div className="gold-price-strip__spread" aria-label="Gold price source">
      <span>Source <strong>{quote?.source ?? 'Gold API'}</strong></span>
    </div>
  </section>;
}
