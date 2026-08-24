import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { DailyProfitChart } from '../src/DailyProfitChart';


describe('DailyProfitChart', () => {
  it('shows the latest daily profit or loss without requiring a tap', async () => {
    render(
      <DailyProfitChart
        days={[
          {
            day: '2026-08-24',
            pnl: 99.17,
            opening_balance: 1000,
            return_percent: 9.917,
          },
        ]}
        currency="USD"
        timezoneName="Europe/Sofia"
        currentBalance={1099.17}
        currentMonthPnl={99.17}
        allTimePnl={99.17}
        ownerDemo
      />,
    );

    expect(await screen.findByText('Latest daily result')).toBeInTheDocument();
    expect(screen.getByText(/\+\$99\.17|\+$99\.17|\$99\.17/)).toBeInTheDocument();
    expect(screen.getByText(/Monday, 24 August 2026/)).toBeInTheDocument();
  });
});
