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
    managed_by_this_reader: true,
  },
  {
    chat_id: -222,
    title: 'Trading Room',
    kind: 'group',
    selected: false,
    source_id: null,
    status: null,
    managed_by_this_reader: true,
  },
  {
    chat_id: 777,
    title: 'Private Friend',
    kind: 'user',
    selected: false,
    source_id: null,
    status: null,
    managed_by_this_reader: true,
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

  it('shows the shared catalogue and only loads this reader groups after an explicit request', async () => {
    const shared = [
      {
        source_id: '11111111-1111-4111-8111-111111111111',
        chat_id: -900,
        title: 'Friend Added Signals',
        status: 'paused',
      },
    ];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, [account]))
      .mockResolvedValueOnce(jsonResponse(200, shared))
      .mockResolvedValueOnce(jsonResponse(403, {}))
      .mockResolvedValueOnce(jsonResponse(200, available))
      .mockResolvedValueOnce(jsonResponse(200, shared));
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramSourceSelector apiBaseUrl="/api" />);

    fireEvent.click(screen.getByRole('button', { name: 'Manage signal sources' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(await screen.findByText('Friend Added Signals')).toBeInTheDocument();
    expect(screen.queryByText('Gold Signals')).not.toBeInTheDocument();
    expect(screen.queryByText('Source-state alerts')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show groups & channels' }));

    expect(await screen.findByText('Gold Signals')).toBeInTheDocument();
    expect(screen.getByText('Trading Room')).toBeInTheDocument();
    expect(screen.queryByText('Private Friend')).not.toBeInTheDocument();
    expect(screen.getByText(/Telegram sessions remain private/i)).toBeInTheDocument();
  });

  it('changes a shared source between Day 11 states immediately', async () => {
    const source = {
      source_id: '11111111-1111-4111-8111-111111111111',
      chat_id: -900,
      title: 'Gold Signals',
      status: 'paused',
    };
    const changed = {
      source_id: source.source_id,
      title: source.title,
      previous_status: 'paused',
      status: 'testing',
      changed_at: '2026-08-08T10:00:00Z',
      actor_display_name: 'Danny',
      actor_role: 'owner',
      monitoring_started: false,
      live_trading_enabled: false,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, [account]))
      .mockResolvedValueOnce(jsonResponse(200, [source]))
      .mockResolvedValueOnce(jsonResponse(200, []))
      .mockResolvedValueOnce(jsonResponse(200, changed))
      .mockResolvedValueOnce(jsonResponse(200, []));
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramSourceSelector apiBaseUrl="/api" />);
    fireEvent.click(screen.getByRole('button', { name: 'Manage signal sources' }));
    expect(await screen.findByText('SHARED · PAUSED')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Testing' }));

    expect(await screen.findByText('SHARED · TESTING')).toBeInTheDocument();
    expect(
      screen.getByText(/moved from PAUSED to TESTING\. The change was audited/i),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      `/api/admin/telegram/sources/shared/${source.source_id}/status`,
      expect.objectContaining({
        method: 'PATCH',
        credentials: 'include',
        body: JSON.stringify({ status: 'testing' }),
      }),
    );
  });

  it('shows persisted Trading Admin source-state alerts only when the owner endpoint allows it', async () => {
    const source = {
      source_id: '11111111-1111-4111-8111-111111111111',
      chat_id: -900,
      title: 'Gold Signals',
      status: 'live',
    };
    const alerts = [
      {
        event_id: 91,
        source_id: source.source_id,
        title: source.title,
        previous_status: 'testing',
        status: 'live',
        actor_display_name: 'Rikke',
        changed_at: '2026-08-08T10:10:00Z',
      },
    ];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, [account]))
      .mockResolvedValueOnce(jsonResponse(200, [source]))
      .mockResolvedValueOnce(jsonResponse(200, alerts));
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramSourceSelector apiBaseUrl="/api" />);
    fireEvent.click(screen.getByRole('button', { name: 'Manage signal sources' }));

    expect(await screen.findByText('Source-state alerts')).toBeInTheDocument();
    expect(screen.getByText('Rikke changed Gold Signals to LIVE')).toBeInTheDocument();
    expect(screen.getByText(/TESTING → LIVE/)).toBeInTheDocument();
  });

  it('adds and removes this reader while keeping the shared catalogue consistent', async () => {
    const sourceId = '4e29f2e9-855b-4e2a-9462-d684e69fc8db';
    let linked = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/admin/telegram/accounts')) return jsonResponse(200, [account]);
      if (url.endsWith('/admin/telegram/sources/owner-alerts')) return jsonResponse(403, {});
      if (url.endsWith('/admin/telegram/sources/shared')) {
        return jsonResponse(
          200,
          linked
            ? [{ source_id: sourceId, chat_id: -100111, title: 'Gold Signals', status: 'paused' }]
            : [],
        );
      }
      if (url.endsWith(`/admin/telegram/sources/accounts/${account.id}/available`)) {
        return jsonResponse(
          200,
          available.slice(0, 2).map((source) =>
            source.chat_id === -100111
              ? {
                  ...source,
                  selected: linked,
                  source_id: linked ? sourceId : null,
                  status: linked ? 'paused' : null,
                  managed_by_this_reader: true,
                }
              : source,
          ),
        );
      }
      if (url.endsWith(`/admin/telegram/sources/accounts/${account.id}/select`) && init?.method === 'POST') {
        linked = true;
        return jsonResponse(201, {
          ...available[0],
          selected: true,
          source_id: sourceId,
          status: 'paused',
          managed_by_this_reader: true,
        });
      }
      if (url.endsWith(`/admin/telegram/sources/accounts/${account.id}/selected/${sourceId}/remove`) && init?.method === 'POST') {
        linked = false;
        return jsonResponse(200, { removed: true, source_id: sourceId, monitoring_started: false });
      }
      return jsonResponse(404, {});
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramSourceSelector apiBaseUrl="/api" />);
    fireEvent.click(screen.getByRole('button', { name: 'Manage signal sources' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Show groups & channels' }));
    await screen.findByText('Gold Signals');

    fireEvent.click(screen.getAllByRole('button', { name: 'Add to shared sources' })[0]);

    expect(await screen.findByText('SELECTED · PAUSED')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/admin/telegram/sources/accounts/${account.id}/select`,
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ chat_id: -100111 }),
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Remove my reader' }));
    await waitFor(() => expect(screen.queryByText('SELECTED · PAUSED')).not.toBeInTheDocument());
  });

  it('adds this private reader as a fallback without duplicating an existing shared source', async () => {
    const sourceId = '22222222-2222-4222-8222-222222222222';
    let managedByThisReader = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/admin/telegram/accounts')) return jsonResponse(200, [account]);
      if (url.endsWith('/admin/telegram/sources/owner-alerts')) return jsonResponse(403, {});
      if (url.endsWith('/admin/telegram/sources/shared')) {
        return jsonResponse(200, [{ source_id: sourceId, chat_id: -100111, title: 'Gold Signals', status: 'paused' }]);
      }
      if (url.endsWith(`/admin/telegram/sources/accounts/${account.id}/available`)) {
        return jsonResponse(200, [
          {
            ...available[0],
            selected: true,
            source_id: sourceId,
            status: 'paused',
            managed_by_this_reader: managedByThisReader,
          },
        ]);
      }
      if (url.endsWith(`/admin/telegram/sources/accounts/${account.id}/select`) && init?.method === 'POST') {
        managedByThisReader = true;
        return jsonResponse(201, {
          ...available[0],
          selected: true,
          source_id: sourceId,
          status: 'paused',
          managed_by_this_reader: true,
        });
      }
      return jsonResponse(404, {});
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramSourceSelector apiBaseUrl="/api" />);
    fireEvent.click(screen.getByRole('button', { name: 'Manage signal sources' }));
    expect(await screen.findByText('Gold Signals')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show groups & channels' }));

    expect(await screen.findByText('ALREADY SHARED · PAUSED')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add my reader too' }));

    expect(await screen.findByText('SELECTED · PAUSED')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove my reader' })).toBeInTheDocument();
  });
});
