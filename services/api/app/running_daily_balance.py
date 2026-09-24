"""Shared 21:00-Sofia equity calendar for website/member reporting.

The owner-facing account total is Vantage/MT5 equity: closed balance plus floating P/L.
Each reported day is the full-account-value movement from 21:00 Europe/Sofia on the
previous calendar date to 21:00 Europe/Sofia on the reported date. The current reported
day remains live until its 21:00 boundary.

This module is read-only and never changes execution, provider risk, or broker state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

SOFIA = ZoneInfo("Europe/Sofia")
RUNNING_ACCOUNT_VALUE_START_DAY = date(2026, 9, 23)
_TRADING_DAY_CUTOFF = time(21, 0)


def _money(value: Decimal | str | float | int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _reporting_day(point: datetime) -> date:
    local = point.astimezone(SOFIA)
    if local.timetz().replace(tzinfo=None) >= _TRADING_DAY_CUTOFF:
        return local.date() + timedelta(days=1)
    return local.date()


def _day_start_utc(day: date) -> datetime:
    """Start of reported day: 21:00 Sofia on the previous calendar date."""
    return datetime.combine(
        day - timedelta(days=1),
        _TRADING_DAY_CUTOFF,
        tzinfo=SOFIA,
    ).astimezone(UTC)


def _day_end_utc(day: date) -> datetime:
    """End of reported day: 21:00 Sofia on the reported calendar date."""
    return datetime.combine(day, _TRADING_DAY_CUTOFF, tzinfo=SOFIA).astimezone(UTC)


def _snapshot_equity_at_or_before(
    session: Session,
    user_id: UUID,
    point: datetime,
) -> Decimal | None:
    row = session.execute(
        text(
            """
            SELECT pas.equity AS account_value
            FROM performance_account_snapshots pas
            JOIN mt5_accounts a ON a.id=pas.mt5_account_id
            WHERE a.owner_user_id=:user_id
              AND a.status<>'revoked'
              AND pas.captured_at<=:point
              AND pas.equity IS NOT NULL
            ORDER BY pas.captured_at DESC
            LIMIT 1
            """
        ),
        {"user_id": user_id, "point": point},
    ).mappings().first()
    if row is None or row.get("account_value") is None:
        return None
    return _money(row["account_value"])


def opening_account_value(
    session: Session,
    user_id: UUID,
    day: date,
) -> Decimal | None:
    """Full Vantage account value at the day's 21:00-Sofia opening boundary."""
    return _snapshot_equity_at_or_before(session, user_id, _day_start_utc(day))


@dataclass(frozen=True, slots=True)
class RunningAccountDay:
    day: date
    opening_value: Decimal
    closing_value: Decimal

    @property
    def pnl(self) -> Decimal:
        return _money(self.closing_value - self.opening_value)


def account_value_days(
    session: Session,
    user_id: UUID,
    *,
    now: datetime | None = None,
) -> tuple[RunningAccountDay, ...]:
    """Return 21:00-to-21:00 full-equity days from the live cutover onward.

    This is intentionally one database round-trip. The previous implementation issued
    two snapshot queries per day, which made the public website wait several seconds on
    cold Render/Postgres reads.
    """
    point = (now or datetime.now(UTC)).astimezone(UTC)
    last_day = _reporting_day(point)
    if last_day < RUNNING_ACCOUNT_VALUE_START_DAY:
        return ()

    rows = session.execute(
        text(
            """
            WITH days AS (
                SELECT gs::date AS day
                FROM generate_series(
                    CAST(:start_day AS date),
                    CAST(:last_day AS date),
                    INTERVAL '1 day'
                ) AS gs
            ),
            boundaries AS (
                SELECT
                    day,
                    (
                        ((day - 1)::timestamp + TIME '21:00')
                        AT TIME ZONE 'Europe/Sofia'
                    ) AS opening_at,
                    LEAST(
                        (
                            (day::timestamp + TIME '21:00')
                            AT TIME ZONE 'Europe/Sofia'
                        ),
                        CAST(:point AS timestamptz)
                    ) AS closing_at
                FROM days
            )
            SELECT
                b.day,
                opening.equity AS opening_value,
                closing.equity AS closing_value
            FROM boundaries b
            LEFT JOIN LATERAL (
                SELECT pas.equity
                FROM performance_account_snapshots pas
                WHERE pas.user_id=:user_id
                  AND pas.captured_at<=b.opening_at
                  AND pas.equity IS NOT NULL
                ORDER BY pas.captured_at DESC
                LIMIT 1
            ) opening ON TRUE
            LEFT JOIN LATERAL (
                SELECT pas.equity
                FROM performance_account_snapshots pas
                WHERE pas.user_id=:user_id
                  AND pas.captured_at<=b.closing_at
                  AND pas.equity IS NOT NULL
                ORDER BY pas.captured_at DESC
                LIMIT 1
            ) closing ON TRUE
            ORDER BY b.day
            """
        ),
        {
            "start_day": RUNNING_ACCOUNT_VALUE_START_DAY,
            "last_day": last_day,
            "point": point,
            "user_id": user_id,
        },
    ).mappings().all()

    return tuple(
        RunningAccountDay(
            day=row["day"],
            opening_value=_money(row["opening_value"]),
            closing_value=_money(row["closing_value"]),
        )
        for row in rows
        if row["opening_value"] is not None and row["closing_value"] is not None
    )


__all__ = [
    "RUNNING_ACCOUNT_VALUE_START_DAY",
    "RunningAccountDay",
    "account_value_days",
    "opening_account_value",
]
