import { useState } from 'react';

type CloseResponse = {
  requested_count: number;
  closed_count: number;
  already_closed_count: number;
  failed_count: number;
};

type PositionProps = {
  apiBaseUrl: string;
  positionId: string;
  symbol: string;
  tpIndex: number;
  disabled?: boolean;
};

type CloseAllProps = {
  apiBaseUrl: string;
  openCount: number;
};

type ApiDetail = { code?: unknown; message?: unknown };

const TRANSIENT_CLOSE_STATUSES = new Set([502, 503, 504]);
const CLOSE_RETRY_DELAYS_MS = [500, 1200, 2500, 4500];
const ALREADY_CLOSED_CODES = new Set([
  'owner_manual_position_not_open',
  'owner_manual_trade_not_open',
  'owner_manual_all_not_open',
]);

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function detailFrom(body: unknown): ApiDetail | null {
  if (typeof body !== 'object' || body === null || !('detail' in body)) return null;
  const detail = (body as { detail: unknown }).detail;
  return typeof detail === 'object' && detail !== null ? detail as ApiDetail : null;
}

function apiError(body: unknown, fallback: string): string {
  const detail = detailFrom(body);
  return detail && typeof detail.message === 'string' ? detail.message : fallback;
}

function apiErrorCode(body: unknown): string | null {
  const detail = detailFrom(body);
  return detail && typeof detail.code === 'string' ? detail.code : null;
}

function alreadyClosedResult(): CloseResponse {
  return {
    requested_count: 0,
    closed_count: 0,
    already_closed_count: 1,
    failed_count: 0,
  };
}

/**
 * Manual close is the one mutation allowed to retry through a transient Render edge
 * failure. The backend serializes each mapped broker position with PostgreSQL advisory
 * locks and re-checks local + broker state before every mutation, so a lost HTTP response
 * cannot turn a retry into a second independent close.
 */
async function closeAtMarket(apiBaseUrl: string, endpoint: string): Promise<CloseResponse> {
  for (let attempt = 0; attempt <= CLOSE_RETRY_DELAYS_MS.length; attempt += 1) {
    try {
      const response = await fetch(`${apiBaseUrl}${endpoint}`, {
        method: 'POST',
        credentials: 'include',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
      });
      const body = (await response.json().catch(() => null)) as CloseResponse | unknown;
      const code = apiErrorCode(body);

      if (response.ok) return body as CloseResponse;
      if (code && ALREADY_CLOSED_CODES.has(code)) return alreadyClosedResult();

      const transient = TRANSIENT_CLOSE_STATUSES.has(response.status)
        || code === 'api_temporarily_unavailable';
      if (transient && attempt < CLOSE_RETRY_DELAYS_MS.length) {
        await wait(CLOSE_RETRY_DELAYS_MS[attempt]);
        continue;
      }
      if (transient) {
        throw new Error('The close could not be confirmed after safe retries. Check MT5 before pressing Close now again.');
      }
      throw new Error(apiError(body, 'The broker did not confirm the close.'));
    } catch (caught) {
      const isOurError = caught instanceof Error
        && (
          caught.message === 'The close could not be confirmed after safe retries. Check MT5 before pressing Close now again.'
          || caught.message === 'The broker did not confirm the close.'
        );
      if (isOurError) throw caught;
      if (attempt < CLOSE_RETRY_DELAYS_MS.length) {
        await wait(CLOSE_RETRY_DELAYS_MS[attempt]);
        continue;
      }
      throw new Error('The close could not reach Smart Signals after safe retries. Check MT5 before pressing Close now again.');
    }
  }
  throw new Error('The close could not be confirmed. Check MT5 before trying again.');
}

export function OwnerPositionCloseButton({ apiBaseUrl, positionId, symbol, tpIndex, disabled = false }: PositionProps) {
  const [closing, setClosing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const close = async () => {
    if (!window.confirm(`Close ${symbol} TP${tpIndex} now at the broker's current market price?`)) return;
    setClosing(true);
    setError(null);
    try {
      const result = await closeAtMarket(apiBaseUrl, `/account/mt5/owner-close-position/${positionId}`);
      if (result.failed_count > 0) {
        setError('The broker did not confirm this close. The position remains open.');
        return;
      }
      window.dispatchEvent(new Event('super-signals-ledger-synced'));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The broker did not confirm the close.');
    } finally {
      setClosing(false);
    }
  };

  return <div className="owner-inline-close">
    <button type="button" className="owner-inline-close__position" disabled={disabled || closing} onClick={() => void close()}>{closing ? 'Closing safely…' : 'Close now'}</button>
    {error && <small className="owner-inline-close__error" role="alert">{error}</small>}
  </div>;
}

export function OwnerCloseAllButton({ apiBaseUrl, openCount }: CloseAllProps) {
  const [closing, setClosing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const closeAll = async () => {
    if (!window.confirm(`Close ALL ${openCount} open Smart Signals positions now at the broker's current market price?`)) return;
    setClosing(true);
    setError(null);
    try {
      const result = await closeAtMarket(apiBaseUrl, '/account/mt5/owner-close-all');
      if (result.failed_count > 0) {
        setError(`${result.closed_count} closed, but ${result.failed_count} could not be confirmed and remain open.`);
      }
      window.dispatchEvent(new Event('super-signals-ledger-synced'));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The broker did not confirm the close-all request.');
    } finally {
      setClosing(false);
    }
  };

  return <div className="owner-close-all">
    <button type="button" disabled={closing} onClick={() => void closeAll()}>{closing ? 'Closing all safely…' : `Close all ${openCount} open positions now`}</button>
    <small>Owner Admin · Demo only · closes mapped Smart Signals positions at market</small>
    {error && <small className="owner-inline-close__error" role="alert">{error}</small>}
  </div>;
}
