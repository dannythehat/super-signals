import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { App } from '../src/App';

describe('App', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders the foundation screen and confirms API health', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          status: 'healthy',
          service: 'super-signals-api',
          version: '0.1.0',
          environment: 'test',
        }),
      }),
    );

    render(<App />);

    expect(screen.getByRole('heading', { name: 'Super Signals' })).toBeInTheDocument();
    expect(await screen.findByText('API connected')).toBeInTheDocument();
    expect(
      screen.getByText('Demo foundation only. No live trading is enabled.'),
    ).toBeInTheDocument();
  });

  it('shows a safe unavailable state when the API cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network unavailable')));

    render(<App />);

    expect(await screen.findByText('API unavailable')).toBeInTheDocument();
  });
});
