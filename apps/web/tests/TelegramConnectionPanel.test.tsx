import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TelegramConnectionPanel } from '../src/TelegramConnectionPanel';

const authorization = {
  flow_id: '937a1a41-74f2-4cab-9016-16d50cbc7e78',
  status: 'pending',
  qr_url: 'tg://login?token=private-test-token',
  qr_image_data_uri: 'data:image/svg+xml;base64,PHN2Zz48cGF0aC8+PC9zdmc+',
  expires_at: '2026-08-07T04:00:00Z',
};

const connectedAccount = {
  id: 'd1fcbd4b-2546-43eb-a424-b4d42a6dbf02',
  label: 'Primary signal reader',
  phone_hint: '+35***567',
  status: 'connected',
  last_connected_at: '2026-08-07T03:59:00Z',
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe('TelegramConnectionPanel', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('creates and displays a short-lived scannable Telegram QR', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, []))
      .mockResolvedValueOnce(jsonResponse(201, authorization));
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramConnectionPanel apiBaseUrl="/api" />);

    fireEvent.click(screen.getByRole('button', { name: 'Manage Telegram accounts' }));
    expect(
      await screen.findByText('No Telegram reader account is connected yet.'),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Private account label'), {
      target: { value: 'Primary signal reader' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create secure QR' }));

    const qrImage = await screen.findByRole('img', {
      name: 'Telegram authorisation QR code',
    });
    expect(qrImage).toHaveAttribute('src', authorization.qr_image_data_uri);
    expect(screen.getByRole('link', { name: 'Open in Telegram' })).toHaveAttribute(
      'href',
      authorization.qr_url,
    );
    expect(
      screen.getByText('This QR is never saved to the database or audit log and expires automatically.'),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/admin/telegram/accounts/authorize',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ label: 'Primary signal reader' }),
      }),
    );
  });

  it('completes Telegram two-step verification without exposing the password', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, []))
      .mockResolvedValueOnce(jsonResponse(201, authorization))
      .mockResolvedValueOnce(
        jsonResponse(200, {
          flow_id: authorization.flow_id,
          status: 'password_required',
          account: null,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          flow_id: authorization.flow_id,
          status: 'connected',
          account: connectedAccount,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, [connectedAccount]));
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramConnectionPanel apiBaseUrl="/api" />);

    fireEvent.click(screen.getByRole('button', { name: 'Manage Telegram accounts' }));
    await screen.findByText('No Telegram reader account is connected yet.');
    fireEvent.change(screen.getByLabelText('Private account label'), {
      target: { value: 'Primary signal reader' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create secure QR' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Check connection' }));

    const passwordInput = await screen.findByLabelText('Telegram two-step password');
    fireEvent.change(passwordInput, {
      target: { value: 'private telegram password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Complete protected sign-in' }));

    expect(
      await screen.findByText('Telegram connected and the encrypted server session was saved.'),
    ).toBeInTheDocument();
    expect(await screen.findByText('Primary signal reader')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      `/api/admin/telegram/accounts/authorize/${authorization.flow_id}/password`,
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ password: 'private telegram password' }),
      }),
    );
    expect(screen.queryByDisplayValue('private telegram password')).not.toBeInTheDocument();
  });

  it('requires confirmation before destroying a connected server session', async () => {
    const disconnectedAccount = {
      ...connectedAccount,
      status: 'disconnected',
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, [connectedAccount]))
      .mockResolvedValueOnce(
        jsonResponse(200, {
          disconnected: true,
          server_session_destroyed: true,
          remote_logout: true,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, [disconnectedAccount]));
    vi.stubGlobal('fetch', fetchMock);

    render(<TelegramConnectionPanel apiBaseUrl="/api" />);

    fireEvent.click(screen.getByRole('button', { name: 'Manage Telegram accounts' }));
    expect(await screen.findByText('Primary signal reader')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Confirm disconnect' }));

    expect(
      await screen.findByText(
        'Telegram logged out remotely and the saved server session was destroyed.',
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenNthCalledWith(
        2,
        `/api/admin/telegram/accounts/${connectedAccount.id}/disconnect`,
        expect.objectContaining({ method: 'POST', credentials: 'include' }),
      );
    });
    expect(screen.getByText('disconnected')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();
  });
});
