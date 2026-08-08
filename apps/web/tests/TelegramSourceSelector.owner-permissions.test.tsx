import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TelegramSourceSelector } from '../src/TelegramSourceSelector';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe('TelegramSourceSelector owner-alert permissions', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('does not probe the Owner-only alert endpoint for a non-owner admin', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(200, [
          {
            id: 'd1fcbd4b-2546-43eb-a424-b4d42a6dbf02',
            label: 'Rikke reader',
            phone_hint: '+45***123',
            status: 'connected',
          },
        ]),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, [
          {
            source_id: '11111111-1111-4111-8111-111111111111',
            chat_id: -100111,
            title: 'Gold Signals',
            status: 'paused',
          },
        ]),
      );
    vi.stubGlobal('fetch', fetchMock);

    render(
      <TelegramSourceSelector apiBaseUrl="/api" canViewOwnerAlerts={false} />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Manage signal sources' }));

    expect(await screen.findByText('Gold Signals')).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes('/owner-alerts')),
    ).toBe(false);
    expect(screen.queryByText('Source-state alerts')).not.toBeInTheDocument();
  });
});
