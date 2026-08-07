import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TelegramSourceSelector } from '../src/TelegramSourceSelector';

const account = {
  id: 'd1fcbd4b-2546-43eb-a424-b4d42a6dbf02',
  label: 'Primary signal reader',
  phone_hint: '+35***567',
  status: 'connected',
  last_connected_at: '2026-08-07T16:10:00Z',
};

const available = [
  {
    chat_id: -100111,
    title: 'Gold Signals',
    kind: 'channel',
    selected: false,
    source_id: null,
    status: null,
  },
  {
    chat_id: -222,
    title: 'Trading Room',
    kind: 'group',
    selected: false,
    source_id: null,
    status: null,
  },
  {
    chat_id: 777,
    title: 'Private Friend',
    kind: 'user',
    selected: false,
    source_id: null,
    status: null,
  },
];

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe('TelegramSourceSelector', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows groups/channels only and does not load them before an explicit request', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, [account]))
      .mockResolvedValueOnce(jsonResponse(200, available));
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramSourceSelector apiBaseUrl="/api" />);

    fireEvent.click(screen.getByRole('button', { name: 'Manage signal sources' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('Gold Signals')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show groups & channels' }));

    expect(await screen.findByText('Gold Signals')).toBeInTheDocument();
    expect(screen.getByText('Trading Room')).toBeInTheDocument();
    expect(screen.queryByText('Private Friend')).not.toBeInTheDocument();
    expect(
      screen.getByText(/does not inspect, persist or process message content/i),
    ).toBeInTheDocument();
  });

  it('selects a source as paused and can remove the selection', async () => {
    const selectedSource = {
      ...available[0],
      selected: true,
      source_id: '4e29f2e9-855b-4e2a-9462-d684e69fc8db',
      status: 'paused',
    };
    const selectedList = [selectedSource, available[1]];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, [account]))
      .mockResolvedValueOnce(jsonResponse(200, available))
      .mockResolvedValueOnce(jsonResponse(201, selectedSource))
      .mockResolvedValueOnce(jsonResponse(200, selectedList))
      .mockResolvedValueOnce(
        jsonResponse(200, {
          removed: true,
          source_id: selectedSource.source_id,
          monitoring_started: false,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, available.slice(0, 2)));
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramSourceSelector apiBaseUrl="/api" />);
    fireEvent.click(screen.getByRole('button', { name: 'Manage signal sources' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Show groups & channels' }));
    await screen.findByText('Gold Signals');

    const selectButtons = screen.getAllByRole('button', { name: 'Select source' });
    fireEvent.click(selectButtons[0]);

    expect(
      await screen.findByText('Source selected and kept PAUSED. No monitoring has started.'),
    ).toBeInTheDocument();
    expect(await screen.findByText('SELECTED · PAUSED')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `/api/admin/telegram/sources/accounts/${account.id}/select`,
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ chat_id: -100111 }),
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Remove selection' }));
    expect(await screen.findByText('Source selection removed.')).toBeInTheDocument();
    expect(screen.queryByText('SELECTED · PAUSED')).not.toBeInTheDocument();
  });
});
