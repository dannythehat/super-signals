import { CSSProperties, useEffect, useMemo, useState } from 'react';

import './daily-profit-chart.css';
import './monthly-profit-redesign.css';

export type DailyProfitPoint = {
  day: string;
  pnl: number;
  opening_balance: number;
  return_percent: number;
};

type Props = {
  days: DailyProfitPoint[];
  currency: string;
  timezoneName: string;
  currentBalance: number | null;
  currentMonthPnl: number | null;
  allTimePnl: number | null;
  ownerDemo: boolean;
};

type ViewTab = 'daily' | 'monthly' | 'stats';

type DisplayDay = {
  day: string;
  point: DailyProfitPoint | null;
  weekend: boolean;
};

type YearMonth = {
  key: string;
  pnl: number | null;
  live: boolean;
  locked: boolean;
};

const DAILY_CHART_START_DAY = '2026-08-24';
const OWNER_DEMO_START_DAY = '2026-08-08';
const OWNER_DEMO_STARTING_CAPITAL = 1000;
const REPORTING_YEAR = 2026;
const FIRST_WEEK_LENGTH = 7;

function dateFromCalendarDay(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
}

function calendarDay(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function addCalendarDays(value: string, amount: number): string {
  const parsed = dateFromCalendarDay(value);
  if (!parsed) return value;
  parsed.setUTCDate(parsed.getUTCDate() + amount);
  return calendarDay(parsed);
}

function monthKey(value: string): string {
  return value.slice(0, 7);
}

function monthStart(value: string): string {
  return `${value}-01`;
}

function monthEnd(value: string): string {
  const parsed = dateFromCalendarDay(`${value}-01`);
  if (!parsed) return `${value}-01`;
  parsed.setUTCMonth(parsed.getUTCMonth() + 1, 0);
  return calendarDay(parsed);
}

function isWeekendDay(value: string): boolean {
  const parsed = dateFromCalendarDay(value);
  if (!parsed) return false;
  const weekday = parsed.getUTCDay();
  return weekday === 0 || weekday === 6;
}

function dayLabel(value: string, long = false): string {
  const parsed = dateFromCalendarDay(value);
  if (!parsed || Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, long
    ? { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' }
    : { weekday: 'short', day: 'numeric', timeZone: 'UTC' }).format(parsed);
}

function dateLabel(value: string): string {
  const parsed = dateFromCalendarDay(value);
  if (!parsed || Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC',
  }).format(parsed);
}

function monthLabel(value: string, long = false): string {
  const parsed = dateFromCalendarDay(`${value}-01`);
  if (!parsed || Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, long
    ? { month: 'long', year: 'numeric', timeZone: 'UTC' }
    : { month: 'short', year: '2-digit', timeZone: 'UTC' }).format(parsed);
}

function monthOnlyLabel(value: string): string {
  const parsed = dateFromCalendarDay(`${value}-01`);
  if (!parsed || Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { month: 'short', timeZone: 'UTC' }).format(parsed);
}

function money(value: number | null, currency: string, signed = false): string {
  if (value === null || !Number.isFinite(value)) return '—';
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: currency || 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
      signDisplay: signed ? 'exceptZero' : 'auto',
    }).format(value);
  } catch {
    const sign = signed && value > 0 ? '+' : '';
    return `${sign}${currency || '$'} ${value.toFixed(2)}`;
  }
}

function percent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—';
  const formatted = new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    signDisplay: 'exceptZero',
  }).format(value);
  return `${formatted}%`;
}

function pnlClass(value: number | null): string {
  if (value === null || value === 0) return 'is-flat';
  return value > 0 ? 'is-positive' : 'is-negative';
}

function currentYearMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}

function availableMonths(days: DailyProfitPoint[]): string[] {
  return Array.from(new Set(
    days
      .filter((item) => item.day >= DAILY_CHART_START_DAY && !isWeekendDay(item.day))
      .map((item) => monthKey(item.day)),
  )).sort();
}

function chartDays(days: DailyProfitPoint[], selectedMonth: string | null): DisplayDay[] {
  const eligible = days.filter((item) => item.day >= DAILY_CHART_START_DAY);
  const byDay = new Map(eligible.map((item) => [item.day, item]));

  let startDay = DAILY_CHART_START_DAY;
  let endDay = addCalendarDays(DAILY_CHART_START_DAY, FIRST_WEEK_LENGTH - 1);
  if (selectedMonth) {
    const requestedStart = monthStart(selectedMonth);
    startDay = requestedStart > DAILY_CHART_START_DAY ? requestedStart : DAILY_CHART_START_DAY;
    endDay = monthEnd(selectedMonth);
  }

  const values: DisplayDay[] = [];
  let cursor = startDay;
  while (cursor <= endDay) {
    const weekend = isWeekendDay(cursor);
    values.push({
      day: cursor,
      point: weekend ? null : byDay.get(cursor) ?? null,
      weekend,
    });
    cursor = addCalendarDays(cursor, 1);
  }
  return values;
}

function aggregatePostLaunchMonths(days: DailyProfitPoint[]): Map<string, number> {
  const totals = new Map<string, number>();
  days
    .filter((item) => item.day >= DAILY_CHART_START_DAY && !isWeekendDay(item.day))
    .forEach((item) => {
      const key = monthKey(item.day);
      totals.set(key, (totals.get(key) ?? 0) + item.pnl);
    });
  return totals;
}

function buildYearMonths(days: DailyProfitPoint[], currentMonthPnl: number | null): YearMonth[] {
  const totals = aggregatePostLaunchMonths(days);
  const current = currentYearMonth();
  return Array.from({ length: 12 }, (_, index) => {
    const month = index + 1;
    const key = `${REPORTING_YEAR}-${String(month).padStart(2, '0')}`;
    let pnl = totals.has(key) ? totals.get(key) ?? null : null;
    if (key === current && currentMonthPnl !== null) pnl = currentMonthPnl;
    return {
      key,
      pnl,
      live: key === current && pnl !== null,
      locked: key < current && pnl !== null,
    };
  });
}

export function DailyProfitChart({
  days,
  currency,
  timezoneName,
  currentBalance,
  currentMonthPnl,
  allTimePnl,
  ownerDemo,
}: Props) {
  const [activeTab, setActiveTab] = useState<ViewTab>('daily');
  const months = useMemo(() => availableMonths(days), [days]);
  const [selectedMonth, setSelectedMonth] = useState<string | null>(months.length > 0 ? months[months.length - 1] : null);
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const yearMonths = useMemo(() => buildYearMonths(days, currentMonthPnl), [days, currentMonthPnl]);

  useEffect(() => {
    if (months.length === 0) return;
    if (!selectedMonth || !months.includes(selectedMonth)) setSelectedMonth(months[months.length - 1]);
  }, [months, selectedMonth]);

  const displayDays = useMemo(() => chartDays(days, selectedMonth), [days, selectedMonth]);
  const actualDays = useMemo(
    () => displayDays.flatMap((item) => item.point ? [item.point] : []),
    [displayDays],
  );

  useEffect(() => {
    if (actualDays.length === 0) {
      if (selectedDay !== null) setSelectedDay(null);
      return;
    }
    if (!selectedDay || !actualDays.some((item) => item.day === selectedDay)) {
      setSelectedDay(actualDays[actualDays.length - 1].day);
    }
  }, [actualDays, selectedDay]);

  const selected = useMemo(
    () => actualDays.find((item) => item.day === selectedDay) ?? null,
    [actualDays, selectedDay],
  );
  const dailyMaxMagnitude = useMemo(
    () => Math.max(1, ...actualDays.map((item) => Math.abs(item.pnl))),
    [actualDays],
  );
  const monthlyMaxMagnitude = useMemo(
    () => Math.max(1, ...yearMonths.flatMap((item) => item.pnl === null ? [] : [Math.abs(item.pnl)])),
    [yearMonths],
  );
  const focusMonth = useMemo(
    () => yearMonths.find((item) => item.live) ?? [...yearMonths].reverse().find((item) => item.pnl !== null) ?? null,
    [yearMonths],
  );

  const cleanDays = useMemo(
    () => days.filter((item) => item.day >= DAILY_CHART_START_DAY && !isWeekendDay(item.day)),
    [days],
  );
  const firstKnownDay = cleanDays.length > 0 ? cleanDays[0] : null;
  const startingCapital = ownerDemo
    ? OWNER_DEMO_STARTING_CAPITAL
    : firstKnownDay?.opening_balance ?? currentBalance;
  const totalReturn = startingCapital && allTimePnl !== null
    ? (allTimePnl / startingCapital) * 100
    : null;
  const profitableDays = cleanDays.filter((item) => item.pnl > 0).length;
  const losingDays = cleanDays.filter((item) => item.pnl < 0).length;
  const bestDay = cleanDays.length > 0
    ? cleanDays.reduce((best, item) => item.pnl > best.pnl ? item : best)
    : null;
  const worstDay = cleanDays.length > 0
    ? cleanDays.reduce((worst, item) => item.pnl < worst.pnl ? item : worst)
    : null;
  const performanceStart = ownerDemo ? OWNER_DEMO_START_DAY : firstKnownDay?.day ?? null;

  const chooseMonth = (value: string) => {
    setSelectedMonth(value);
    setSelectedDay(null);
  };

  const openMonth = (value: string) => {
    chooseMonth(value);
    setActiveTab('daily');
  };

  return <section className="daily-profit-panel" aria-labelledby="daily-profit-title">
    <div className="daily-profit-head">
      <div>
        <span>Performance</span>
        <h2 id="daily-profit-title">Profit history</h2>
        <p>Your own Super Signals trading results</p>
      </div>
      <small>{timezoneName}</small>
    </div>

    <nav className="profit-view-tabs" aria-label="Performance views">
      <button type="button" className={activeTab === 'daily' ? 'profit-view-tab--selected' : ''} onClick={() => setActiveTab('daily')} aria-pressed={activeTab === 'daily'}>Daily</button>
      <button type="button" className={activeTab === 'monthly' ? 'profit-view-tab--selected' : ''} onClick={() => setActiveTab('monthly')} aria-pressed={activeTab === 'monthly'}>Monthly</button>
      <button type="button" className={activeTab === 'stats' ? 'profit-view-tab--selected' : ''} onClick={() => setActiveTab('stats')} aria-pressed={activeTab === 'stats'}>My stats</button>
    </nav>

    {activeTab === 'daily' && <>
      {months.length > 0 && <nav className="daily-profit-months" aria-label="Daily history by month">
        {months.map((value) => <button
          type="button"
          key={value}
          className={value === selectedMonth ? 'daily-profit-month--selected' : ''}
          onClick={() => chooseMonth(value)}
          aria-pressed={value === selectedMonth}
        >{monthLabel(value)}</button>)}
      </nav>}

      {selected && <div className="daily-profit-detail" aria-live="polite">
        <strong>{selected.day === actualDays[actualDays.length - 1]?.day ? 'Latest daily result' : dayLabel(selected.day, true)}</strong>
        <span className={pnlClass(selected.pnl)}>{money(selected.pnl, currency, true)}</span>
        <span className={pnlClass(selected.return_percent)}>{percent(selected.return_percent)}</span>
        <small>{dayLabel(selected.day, true)} · Opening {money(selected.opening_balance, currency)}</small>
      </div>}

      <div className="daily-profit-chart" aria-label="Daily realised profit and loss chart">
        <div className="daily-profit-zero-line" aria-hidden="true" />
        <div className="daily-profit-bars" style={{ '--daily-count': displayDays.length } as CSSProperties}>
          {displayDays.map(({ day, point, weekend }) => {
            const height = point
              ? Math.max(point.pnl === 0 ? 4 : 10, Math.round((Math.abs(point.pnl) / dailyMaxMagnitude) * 62))
              : 0;
            const style = { '--bar-height': `${height}px` } as CSSProperties;
            const tone = !point ? 'future' : point.pnl > 0 ? 'positive' : point.pnl < 0 ? 'negative' : 'flat';
            const disabled = weekend || !point;
            return <button
              className={`daily-profit-day ${weekend ? 'daily-profit-day--weekend' : ''} ${point && day === selectedDay ? 'daily-profit-day--selected' : ''}`}
              type="button"
              key={day}
              onClick={() => point && !weekend && setSelectedDay(day)}
              disabled={disabled}
              aria-label={weekend
                ? `${dayLabel(day, true)}: non-trading day`
                : point
                  ? `${dayLabel(day, true)}: ${money(point.pnl, currency, true)}, ${percent(point.return_percent)}`
                  : `${dayLabel(day, true)}: no result yet`}
              aria-pressed={point && !weekend ? day === selectedDay : undefined}
            >
              <span className="daily-profit-bar-zone" aria-hidden="true">
                {point && !weekend && <i className={`daily-profit-bar daily-profit-bar--${tone}`} style={style} />}
                {weekend && <i className="daily-profit-weekend-mark" />}
              </span>
              <span className="daily-profit-day-label">{dayLabel(day)}</span>
            </button>;
          })}
        </div>
      </div>

      <div className="daily-profit-foot">
        <span>{actualDays.length === 0 ? 'Daily tracking starts Monday 24 August.' : 'The latest daily P/L is shown above. Tap another completed day to inspect it.'}</span>
        <small>Saturday & Sunday are non-trading days.</small>
      </div>
    </>}

    {activeTab === 'monthly' && <div className="monthly-profit-redesign">
      {focusMonth ? <div className="monthly-profit-highlight">
        <div className="monthly-profit-highlight-copy">
          <span>{monthLabel(focusMonth.key, true)}</span>
          <small>{focusMonth.live ? 'Month to date' : focusMonth.locked ? 'Completed month' : 'Monthly result'}</small>
        </div>
        <strong className={pnlClass(focusMonth.pnl)}>{money(focusMonth.pnl, currency, true)}</strong>
        <small className="monthly-profit-baseline">
          {ownerDemo
            ? 'Based on $1,000 starting capital · 8 Aug 2026'
            : startingCapital !== null && performanceStart
              ? `Based on ${money(startingCapital, currency)} starting capital · ${dateLabel(performanceStart)}`
              : 'Based on your own Super Signals account'}
        </small>
      </div> : <div className="monthly-profit-highlight monthly-profit-highlight--empty">
        <div className="monthly-profit-highlight-copy"><span>Monthly performance</span><small>No completed trading month yet</small></div>
        <strong>—</strong>
      </div>}

      <div className="monthly-profit-subhead">
        <strong>{REPORTING_YEAR}</strong>
        <small>Tap a month with data for its daily breakdown</small>
      </div>

      <div className="monthly-profit-chart monthly-profit-chart--compact" aria-label={`${REPORTING_YEAR} monthly profit and loss`}>
        <div className="monthly-profit-zero-line" aria-hidden="true" />
        <div className="monthly-profit-bars monthly-profit-bars--compact">
          {yearMonths.map((item) => {
            const height = item.pnl === null ? 0 : Math.max(7, Math.round((Math.abs(item.pnl) / monthlyMaxMagnitude) * 38));
            const style = { '--month-bar-height': `${height}px` } as CSSProperties;
            const tone = item.pnl === null ? 'empty' : item.pnl > 0 ? 'positive' : item.pnl < 0 ? 'negative' : 'flat';
            const isFocus = focusMonth?.key === item.key;
            return <button
              type="button"
              key={item.key}
              className={`monthly-profit-month monthly-profit-month--${tone} ${isFocus ? 'monthly-profit-month--focus' : ''}`}
              disabled={item.pnl === null}
              onClick={() => item.pnl !== null && openMonth(item.key)}
              aria-label={item.pnl === null
                ? `${monthLabel(item.key, true)}: no performance data`
                : `${monthLabel(item.key, true)}: ${money(item.pnl, currency, true)}${item.live ? ', month to date' : item.locked ? ', completed month' : ''}`}
            >
              <span className="monthly-profit-bar-zone" aria-hidden="true">
                {item.pnl !== null && <i className={`monthly-profit-bar monthly-profit-bar--${tone}`} style={style} />}
              </span>
              <span>{monthOnlyLabel(item.key)}</span>
              <small>{item.live ? 'MTD' : item.locked ? 'Final' : ''}</small>
            </button>;
          })}
        </div>
      </div>

      <div className="monthly-profit-footnote">
        <span>Green = profit</span><span>Red = loss</span><span>Completed months stay fixed</span>
      </div>
    </div>}

    {activeTab === 'stats' && <>
      <div className="profit-stats-grid">
        <article><span>Starting capital</span><strong>{money(startingCapital, currency)}</strong><small>{performanceStart ? `From ${dateLabel(performanceStart)}` : 'Begins with your first Super Signals trade'}</small></article>
        <article><span>Current balance</span><strong>{money(currentBalance, currency)}</strong><small>Your trading balance now</small></article>
        <article><span>Total P/L</span><strong className={pnlClass(allTimePnl)}>{money(allTimePnl, currency, true)}</strong><small>Realised trading profit / loss</small></article>
        <article><span>Total return</span><strong className={pnlClass(totalReturn)}>{percent(totalReturn)}</strong><small>Against starting capital</small></article>
        <article><span>Best day</span><strong className={pnlClass(bestDay?.pnl ?? null)}>{bestDay ? money(bestDay.pnl, currency, true) : '—'}</strong><small>{bestDay ? dateLabel(bestDay.day) : 'Daily tracking starts Monday'}</small></article>
        <article><span>Worst day</span><strong className={pnlClass(worstDay?.pnl ?? null)}>{worstDay ? money(worstDay.pnl, currency, true) : '—'}</strong><small>{worstDay ? dateLabel(worstDay.day) : 'Daily tracking starts Monday'}</small></article>
        <article><span>Profitable days</span><strong>{profitableDays}</strong><small>Since daily tracking began</small></article>
        <article><span>Losing days</span><strong>{losingDays}</strong><small>Since daily tracking began</small></article>
      </div>
      <p className="monthly-profit-note">
        {ownerDemo
          ? 'Owner demo performance uses the agreed $1,000 starting capital from 8 Aug 2026. Detailed daily statistics start cleanly from Monday 24 Aug 2026.'
          : 'These statistics are calculated only from your account. Deposits and withdrawals are capital movements and are not counted as trading profit or loss.'}
      </p>
    </>}
  </section>;
}