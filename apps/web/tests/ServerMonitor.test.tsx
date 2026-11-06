import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ServerMonitor } from '../src/ServerMonitor';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('shows unavailable positions separately from zero and preserves native cent units for each worker', async () => {
  const missing = { profile: 'demo', worker_name: 'worker-a', worker_online: true, broker_state: 'stale', worker_last_seen: null,
    balance: null, equity: null, free_margin: null, currency: 'USC', trade_allowed: null,
    login: null, server: null, account_updated_at: null, positions_updated_at: new Date().toISOString(), positions: null, gold: null };
  const available = { ...missing, worker_name: 'worker-b', broker_state: 'fresh', positions: [], balance: 1234, equity: 1234 };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ read_at: new Date().toISOString(),
    pu_prime: missing, vantage: available, workers: [missing, available],
    mirror: { mode: 'observe', forward_only_since: '', target_login: '' }, source: 'fixture' }) }));
  render(<ServerMonitor apiBaseUrl="/api" active />);
  expect(await screen.findByText('worker-a')).toBeInTheDocument();
  expect(screen.getByText('worker-b')).toBeInTheDocument();
  expect(screen.getByText('No recent position snapshot.')).toBeInTheDocument();
  expect(screen.getByText('No open positions.')).toBeInTheDocument();
  expect(screen.getAllByText('USC 1,234.00')).toHaveLength(2);
  expect(screen.queryByText('$1,234.00')).not.toBeInTheDocument();
});
