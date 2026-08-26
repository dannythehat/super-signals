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

function apiError(body: unknown, fallback: string): string {
  if (typeof body !== 'object' || body === null || !('detail' in body)) return fallback;
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === 'object' && detail !== null && 'message' in detail) {
    return String((detail as { message: unknown }).message);
  }
  return fallback;
}

async function closeAtMarket(apiBaseUrl: string, endpoint: string): Promise<CloseResponse> {
  const response = await fetch(`${apiBaseUrl}${endpoint}`, {
    method: 'POST',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  const body = (await response.json()) as CloseResponse | unknown;
  if (!response.ok) throw new Error(apiError(body, 'The broker did not confirm the close.'));
  return body as CloseResponse;
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
    <button type="button" className="owner-inline-close__position" disabled={disabled || closing} onClick={() => void close()}>{closing ? 'Closing…' : 'Close now'}</button>
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
    <button type="button" disabled={closing} onClick={() => void closeAll()}>{closing ? 'Closing all…' : `Close all ${openCount} open positions now`}</button>
    <small>Owner Admin · Demo only · closes mapped Smart Signals positions at market</small>
    {error && <small className="owner-inline-close__error" role="alert">{error}</small>}
  </div>;
}
