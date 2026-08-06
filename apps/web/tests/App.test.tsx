import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { App } from '../src/App';

const owner = {
  id: '9e5d24de-d6d5-4dad-b6bd-8ff43fbb13c6',
  email: 'owner@example.com',
  display_name: 'Danny',
  role: 'owner',
  security: {
    two_factor: 'setup_required',
    passkey: 'setup_available',
  },
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
  });

  it('shows the owner login when no valid session exists', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, {})));

    render(<App />);

    expect(
      await screen.findByRole('heading', { name: 'Sign in securely' }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Owner email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('shows a safe service error without exposing technical details', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private network details')));

    render(<App />);

    expect(await screen.findByText('The secure service is unavailable.')).toBeInTheDocument();
    expect(screen.queryByText('private network details')).not.toBeInTheDocument();
  });

  it('signs the owner in and reaches the protected dashboard', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, {}))
      .mockResolvedValueOnce(jsonResponse(200, owner));
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    fireEvent.change(await screen.findByLabelText('Owner email'), {
      target: { value: 'owner@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'correct horse battery staple' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(
      await screen.findByRole('heading', { name: 'Welcome, Danny' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Securely signed in')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/auth/login',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
      }),
    );
  });

  it('revokes the visible session and returns to login on logout', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, owner))
      .mockResolvedValueOnce(jsonResponse(204, {}));
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    fireEvent.click(await screen.findByRole('button', { name: 'Log out' }));

    expect(
      await screen.findByRole('heading', { name: 'Sign in securely' }),
    ).toBeInTheDocument();
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

    fireEvent.change(screen.getByLabelText('Owner email'), {
      target: { value: 'unknown@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send recovery instructions' }));

    expect(
      await screen.findByText('If the account is eligible, recovery instructions will be sent.'),
    ).toBeInTheDocument();
  });
});
