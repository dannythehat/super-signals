import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AdminMemberControlsDay35 } from '../src/AdminMemberControlsDay35';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
  } as Response;
}

function textResponse(status: number): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': 'text/plain; charset=utf-8' }),
    json: async () => { throw new SyntaxError("Unexpected token 'I'"); },
  } as unknown as Response;
}

const userId = '2791cc0b-d501-4e51-8b96-72262f11df0b';
const managedUser = {
  user_id: userId,
  email: 'member@example.com',
  display_name: 'Member',
  status: 'active',
  trading_status: 'active',
  risk_percent: 1,
  allow_double_lot: false,
  mt5_status: 'connected',
  mt5_login_masked: '***1234',
  mt5_server: 'VantageInternational-Demo',
  mapped_open_positions: 0,
  mapped_pending_positions: 0,
  active_sessions: 1,
  push_devices_enabled: 1,
};
const activeAccess = {
  user_id: userId,
  email: 'member@example.com',
  display_name: 'Member',
  status: 'active',
  active: true,
  plan_code: 'complimentary',
  active_until: null,
};

describe('AdminMemberControlsDay35', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('uses the mounted owner subscription routes for list and pause', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/access/day35/user-controls/users') return jsonResponse(200, [managedUser]);
      if (url === '/api/owner/mt5/approvals/subscriptions/members') return jsonResponse(200, [activeAccess]);
      if (url === `/api/owner/mt5/approvals/subscriptions/users/${userId}/pause` && init?.method === 'POST') {
        return jsonResponse(200, {
          ...activeAccess,
          status: 'suspended',
          active: false,
          message: 'Subscription paused.',
        });
      }
      return jsonResponse(404, { detail: 'Not Found' });
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<AdminMemberControlsDay35 apiBaseUrl="/api" />);

    expect(await screen.findByText('member@example.com')).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/owner/mt5/approvals/subscriptions/members',
      expect.objectContaining({ credentials: 'include', cache: 'no-store' }),
    ));

    fireEvent.click(screen.getByRole('button', { name: 'Pause subscription' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/owner/mt5/approvals/subscriptions/users/${userId}/pause`,
      expect.objectContaining({ method: 'POST', credentials: 'include' }),
    ));
  });

  it('shows a clean message instead of a JSON parser failure on a server error', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/access/day35/user-controls/users') return jsonResponse(200, [managedUser]);
      if (url === '/api/owner/mt5/approvals/subscriptions/members') return textResponse(500);
      return jsonResponse(404, { detail: 'Not Found' });
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<AdminMemberControlsDay35 apiBaseUrl="/api" />);

    expect(await screen.findByText('Member controls are temporarily unavailable. Please refresh in a moment.')).toBeInTheDocument();
    expect(screen.queryByText(/Unexpected token/)).not.toBeInTheDocument();
    expect(screen.queryByText(/not valid JSON/i)).not.toBeInTheDocument();
  });
});