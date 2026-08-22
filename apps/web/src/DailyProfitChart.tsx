import { CSSProperties, useEffect, useMemo, useState } from 'react';

import './daily-profit-chart.css';

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
};

type DisplayDay = {
  day: string;
  point: DailyProfitPoint | null;
  weekend: boolean;
};

const DAILY_CHART_START_DAY = '2026-08-24';
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

function money(value: number, currency: string, signed = false): string {
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

function percent(value: number): string {
  const formatted = new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    signDisplay: 'exceptZero',
  }).format(value);
  return `${formatted}%`;
}

function pnlClass(value: number): string {
  if (value === 0) return 'is-flat';
  return value > 0 ? 'is-positive' : 'is-negative';
}

function chartDays(days: DailyProfitPoint[]): DisplayDay[] {
  const eligible = days.filter((item) => item.day >= DAILY_CHART_START_DAY);
  const byDay = new Map(eligible.map((item) => [item.day, item]));
  const firstWeekEnd = addCalendarDays(DAILY_CHART_START_DAY, FIRST_WEEK_LENGTH - 1);
  const lastActual = eligible.length > 0 ? eligible[eligible.length - 1].day : DAILY_CHART_START_DAY;
  const endDay = lastActual > firstWeekEnd ? lastActual : firstWeekEnd;
  const values: DisplayDay[] = [];
  let cursor = DAILY_CHART_START_DAY;
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

export function DailyProfitChart({ days, currency, timezoneName }: Props) {
  const displayDays = useMemo(() => chartDays(days), [days]);
  const actualDays = useMemo(
    () => displayDays.flatMap((item) => item.point ? [item.point] : []),
    [displayDays],
  );
  const [selectedDay, setSelectedDay] = useState<string | null>(null);

  useEffect(() => {
    if (selectedDay && !actualDays.some((item) => item.day === selectedDay)) {
      setSelectedDay(null);
    }
  }, [actualDays, selectedDay]);

  const selected = useMemo(
    () => actualDays.find((item) => item.day === selectedDay) ?? null,
    [actualDays, selectedDay],
  );
  const maxMagnitude = useMemo(
    () => Math.max(1, ...actualDays.map((item) => Math.abs(item.pnl))),
    [actualDays],
  );

  return <section className="daily-profit-panel" aria-labelledby="daily-profit-title">
    <div className="daily-profit-head">
      <div>
        <span>Profit history</span>
        <h2 id="daily-profit-title">Daily P/L</h2>
        <p>From Monday 24 August</p>
      </div>
      <small>{timezoneName}</small>
    </div>

    {selected && <div className="daily-profit-detail" aria-live="polite">
      <strong>{dayLabel(selected.day, true)}</strong>
      <span className={pnlClass(selected.pnl)}>{money(selected.pnl, currency, true)}</span>
      <span className={pnlClass(selected.return_percent)}>{percent(selected.return_percent)}</span>
      <small>Opening {money(selected.opening_balance, currency)}</small>
    </div>}

    <div className="daily-profit-chart" aria-label="Daily realised profit and loss chart">
      <div className="daily-profit-zero-line" aria-hidden="true" />
      <div className="daily-profit-bars" style={{ '--daily-count': displayDays.length } as CSSProperties}>
        {displayDays.map(({ day, point, weekend }) => {
          const height = point
            ? Math.max(point.pnl === 0 ? 4 : 10, Math.round((Math.abs(point.pnl) / maxMagnitude) * 62))
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
      <span>{actualDays.length === 0 ? 'Your new profit chart starts Monday.' : 'Tap any completed trading day for its P/L and return.'}</span>
      <small>Saturday & Sunday are non-trading days.</small>
    </div>
  </section>;
}
