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

function dateFromCalendarDay(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
}

function dayLabel(value: string, long = false): string {
  const parsed = dateFromCalendarDay(value);
  if (!parsed || Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, long
    ? { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' }
    : { day: 'numeric', month: 'short', timeZone: 'UTC' }).format(parsed);
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

export function DailyProfitChart({ days, currency, timezoneName }: Props) {
  const [selectedDay, setSelectedDay] = useState<string | null>(days.at(-1)?.day ?? null);

  useEffect(() => {
    if (days.length === 0) {
      setSelectedDay(null);
      return;
    }
    if (!selectedDay || !days.some((item) => item.day === selectedDay)) {
      setSelectedDay(days.at(-1)?.day ?? null);
    }
  }, [days, selectedDay]);

  const selected = useMemo(
    () => days.find((item) => item.day === selectedDay) ?? days.at(-1) ?? null,
    [days, selectedDay],
  );
  const maxMagnitude = useMemo(
    () => Math.max(1, ...days.map((item) => Math.abs(item.pnl))),
    [days],
  );

  return <section className="daily-profit-panel" aria-labelledby="daily-profit-title">
    <div className="daily-profit-head">
      <div><span>Profit history</span><h2 id="daily-profit-title">Daily profit / loss</h2></div>
      <small>{timezoneName}</small>
    </div>

    {selected ? <div className="daily-profit-selected" aria-live="polite">
      <div><span>{dayLabel(selected.day, true)}</span><strong className={pnlClass(selected.pnl)}>{money(selected.pnl, currency, true)}</strong></div>
      <div><span>Daily return</span><strong className={pnlClass(selected.return_percent)}>{percent(selected.return_percent)}</strong></div>
      <div><span>Opening balance</span><strong>{money(selected.opening_balance, currency)}</strong></div>
    </div> : <div className="daily-profit-empty">Daily profit history will appear after the first Super Signals trade.</div>}

    {days.length > 0 && <div className="daily-profit-scroll" aria-label="Daily realised profit and loss chart">
      <div className="daily-profit-bars" style={{ '--daily-count': days.length } as CSSProperties}>
        {days.map((item) => {
          const height = Math.max(item.pnl === 0 ? 3 : 7, Math.round((Math.abs(item.pnl) / maxMagnitude) * 68));
          const style = { '--bar-height': `${height}px` } as CSSProperties;
          return <button
            className={`daily-profit-day ${item.day === selected?.day ? 'daily-profit-day--selected' : ''}`}
            type="button"
            key={item.day}
            onClick={() => setSelectedDay(item.day)}
            aria-label={`${dayLabel(item.day, true)}: ${money(item.pnl, currency, true)}, ${percent(item.return_percent)}`}
            aria-pressed={item.day === selected?.day}
          >
            <span className="daily-profit-bar-zone" aria-hidden="true">
              <i className="daily-profit-axis" />
              <i className={`daily-profit-bar daily-profit-bar--${item.pnl > 0 ? 'positive' : item.pnl < 0 ? 'negative' : 'flat'}`} style={style} />
            </span>
            <span className="daily-profit-day-label">{dayLabel(item.day)}</span>
          </button>;
        })}
      </div>
    </div>}
    <p className="daily-profit-note">Tap any day to see its realised P/L, return on that day's opening balance, and opening balance.</p>
  </section>;
}
