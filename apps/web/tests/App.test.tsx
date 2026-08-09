import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { App } from '../src/App';

const owner = {
  id: '9e5d24de-d6d5-4dad-b6bd-8ff43fbb13c6',
  email: 'owner@example.com',
  display_name: 'Danny',
  role: 'owner',
  role_label: 'Owner Admin',
  roles: ['owner'],
  permissions: ['users.manage', 'sources.manage', 'account.connect'],
  sections: [
    {
      key: 'owner',
      label: 'Owner controls',
      description: 'Users, access, approvals and security.',
      actions: [
        {
          permission: 'users.manage',
          label: 'Invited users',
          description: 'Invite, suspend or revoke users.',
        },
      ],
    },
    {
      key: 'trading',
      label: 'Trading operations',
      description: 'Telegram sources and review activity.',
      actions: [
        {
          permission: 'sources.manage',
          label: 'Signal sources',
          description: 'Add, pause, resume or remove sources.',
        },
      ],
    },
    {
      key: 'user',
      label: 'My trading',
      description: 'Approved account and automation.',
      actions: [
        {
          permission: 'account.connect',
          label: 'MT5 account',
          description: 'Connect one approved account.',
        },
      ],
    },
  ],
  security: {
    two_factor: 'setup_required',
    passkey: 'setup_available',
  },
};

const tradingAdmin = {
  ...owner,
  id: '4fa398b1-65e2-445d-b7ad-08c6d1785f89',
  email: 'trading@example.com',
  display_name: 'Trading Admin',
  role: 'trading_admin',
  role_label: 'Trading Admin',
  roles: ['trading_admin'],
  permissions: ['sources.manage'],
  sections: [owner.sections[1]],
};

const invitedUser = {
  ...owner,
  id: '2791cc0b-d501-4e51-8b96-72262f11df0b',
  email: 'user@example.com',
  display_name: 'Invited User',
  role: 'user',
  role_label: 'Invited User',
  roles: ['user'],
  permissions: ['account.connect'],
  sections: [owner.sections[2]],
};

const sharedSource = {
  source_id: '11111111-1111-4111-8111-111111111111',
  chat_id: -1001651583302,
  title: 'FXTradingVision | Forex & Crypto Signals 🚀',
  status: 'paused',
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe('App', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState({}, '', window.location.href);
  });

  it('shows account login when no valid session exists', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, {})));

    render(<App />);

    expect(await screen.findByRole('heading', { name: 'Sign in securely' })).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('shows a safe service error without exposing technical details', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private network details')));

    render(<App />);

    expect(await screen.findByText('The secure service is unavailable.')).toBeInTheDocument();
    expect(screen.queryByText('private network details')).not.toBeInTheDocument();
  });

  it('signs the owner in to a clean overview with settings behind the menu', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, {}))
      .mockResolvedValueOnce(jsonResponse(200, owner))
      .mockResolvedValueOnce(jsonResponse(200, []));
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    fireEvent.change(await screen.findByLabelText('Email'), {
      target: { value: 'owner@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'correct horse battery staple' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByRole('heading', { name: 'Welcome back, Danny' })).toBeInTheDocument();
    expect(screen.getByText('Live trading disabled')).toBeInTheDocument();
    expect(screen.queryByText('Owner controls')).not.toBeInTheDocument();
    expect(screen.queryByText('Trading operations')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/login',
      expect.objectContaining({ method: 'POST', credentials: 'include' }),
    );
  });

  it('shows connected shared sources on the overview and returns home without browser back', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, owner))
      .mockResolvedValueOnce(jsonResponse(200, [sharedSource]));
    vi.stubGlobal('fetch', fetchMock);
    window.scrollTo = vi.fn();

    render(<App />);

    expect(await screen.findByText(sharedSource.title)).toBeInTheDocument();
    expect(screen.getByText('SHARED · PAUSED')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    fireEvent.click(screen.getByRole('button', { name: /Access & security/i }));

    expect(await screen.findByRole('heading', { name: 'Access & security' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Back to overview' }));
    expect(await screen.findByRole('heading', { name: 'Welcome back, Danny' })).toBeInTheDocument();
  });

  it('opens the workspace menu and navigates to access settings', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, owner)));
    window.scrollTo = vi.fn();

    render(<App />);

    fireEvent.click(await screen.findByRole('button', { name: 'Open menu' }));
    expect(screen.getByRole('button', { name: /Telegram accounts/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Signal sources/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Access & security/i }));

    expect(await screen.findByRole('heading', { name: 'Access & security' })).toBeInTheDocument();
    expect(screen.getByText('Owner controls')).toBeInTheDocument();
    expect(screen.getByText('Trading operations')).toBeInTheDocument();
    expect(screen.getByText('My trading')).toBeInTheDocument();
  });

  it('shows Telegram navigation to a Trading Admin without exposing Owner controls', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, tradingAdmin)));

    render(<App />);

    expect(
      await screen.findByRole('heading', { name: 'Welcome back, Trading Admin' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    expect(screen.getByRole('button', { name: /Telegram accounts/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Signal sources/i })).toBeInTheDocument();
    expect(screen.queryByText('Owner controls')).not.toBeInTheDocument();
  });

  it('keeps Telegram management out of an invited user menu', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, invitedUser)));
    window.scrollTo = vi.fn();

    render(<App />);

    expect(
      await screen.findByRole('heading', { name: 'Welcome back, Invited User' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    expect(screen.queryByRole('button', { name: /Telegram accounts/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Signal sources/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Access & security/i }));
    expect(await screen.findByText('My trading')).toBeInTheDocument();
    expect(screen.queryByText('Owner controls')).not.toBeInTheDocument();
    expect(screen.queryByText('Trading operations')).not.toBeInTheDocument();
  });

  it('revokes the visible session and returns to login on logout', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, owner))
      .mockResolvedValueOnce(jsonResponse(200, []))
      .mockResolvedValueOnce(jsonResponse(204, {}));
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    fireEvent.click(await screen.findByRole('button', { name: 'Open menu' }));
    fireEvent.click(screen.getByRole('button', { name: 'Log out' }));

    expect(await screen.findByRole('heading', { name: 'Sign in securely' })).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenLastCalledWith(
        '/api/auth/logout',
        expect.objectContaining({ method: 'POST', credentials: 'include' }),
      );
    });
  });

  it('uses the same safe recovery response for every email', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, {}))
      .mockResolvedValueOnce(
        jsonResponse(202, {
          message: 'If the account is eligible, recovery instructions will be sent.',
        }),
      );
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    fireEvent.click(await screen.findByRole('button', { name: 'I cannot access my account' }));
    expect(screen.getByRole('heading', { name: 'Recover access' })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Account email'), {
      target: { value: 'unknown@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send recovery instructions' }));

    expect(
      await screen.findByText('If the account is eligible, recovery instructions will be sent.'),
    ).toBeInTheDocument();
  });
});
