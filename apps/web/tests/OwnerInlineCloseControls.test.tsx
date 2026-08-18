import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { OwnerCloseAllButton, OwnerPositionCloseButton } from '../src/OwnerInlineCloseControls';

function response(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

describe('Owner inline manual close controls', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('closes one exact mapped position endpoint', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = vi.fn(async () => response({ requested_count: 1, closed_count: 1, already_closed_count: 0, failed_count: 0 }));
    vi.stubGlobal('fetch', fetchMock);

    render(<OwnerPositionCloseButton apiBaseUrl="" positionId="position-123" symbol="XAUUSD" tpIndex={2} />);
    fireEvent.click(screen.getByRole('button', { name: 'Close now' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/account/mt5/owner-close-position/position-123', expect.objectContaining({ method: 'POST' })));
  });

  it('requires explicit confirmation before closing all open positions', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    render(<OwnerCloseAllButton apiBaseUrl="" openCount={4} />);
    fireEvent.click(screen.getByRole('button', { name: 'Close all 4 open positions now' }));

    expect(confirm).toHaveBeenCalledWith('Close ALL 4 open Super Signals positions now at the broker\'s current market price?');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('uses the dedicated close-all endpoint after confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = vi.fn(async () => response({ requested_count: 3, closed_count: 3, already_closed_count: 0, failed_count: 0 }));
    vi.stubGlobal('fetch', fetchMock);

    render(<OwnerCloseAllButton apiBaseUrl="" openCount={3} />);
    fireEvent.click(screen.getByRole('button', { name: 'Close all 3 open positions now' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/account/mt5/owner-close-all', expect.objectContaining({ method: 'POST' })));
  });
});
