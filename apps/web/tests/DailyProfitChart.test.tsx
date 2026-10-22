import { fireEvent, render, screen } from '@testing-library/react';
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
          {
            day: '2026-09-03',
            pnl: 99.17,
            opening_balance: 1000,
            return_percent: 9.917,
          },
        ]}
        currency="USD"
        timezoneName="Europe/Sofia"
        currentBalance={1099.17}
        currentMonthPnl={99.17}
        allTimePnl={198.34}
        ownerDemo
      />,
    );

    expect(await screen.findByText('Latest daily result')).toBeInTheDocument();
    expect(screen.getAllByText((text) => text.includes('99.17')).length).toBeGreaterThan(0);
    expect(screen.getByText((text) => text.includes('Opening'))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'August 2026' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Aug 26' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Monthly' }));
    expect(screen.getByText((text) => text.includes('9.92%') && text.includes('MTD'))).toBeInTheDocument();
    expect(screen.getAllByText((text) => text.includes('$99') || text.includes('US$99')).length).toBeGreaterThan(0);
  });
});
