import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const source = (name: string) => readFileSync(resolve(here, `../src/${name}`), 'utf8');

describe('dashboard fast path', () => {
  it('never forces broker-history reconciliation from user-facing dashboard components', () => {
    const today = source('TodayTradingSummary.tsx');
    const timeline = source('TradeTimeline.tsx');

    expect(today).not.toContain('/dashboard/performance/sync');
    expect(timeline).not.toContain('/dashboard/performance/sync');
    expect(today).not.toContain('super-signals-ledger-synced');
    expect(timeline).not.toContain('super-signals-ledger-synced');
  });

  it('does not poll the Today endpoint every five seconds', () => {
    const today = source('TodayTradingSummary.tsx');
    expect(today).not.toContain('5000');
    expect(today).toContain('15_000');
  });
});
