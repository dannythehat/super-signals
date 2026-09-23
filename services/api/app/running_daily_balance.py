"""Shared running daily closed-balance P/L for public website and Telegram.

"Balance" means the broker's MT5/Vantage balance field: closed cash only. Open/pending
positions and floating P/L affect equity, not balance, and must not change today's
published balance P/L. Historical realised-trade P/L remains available elsewhere for
audit. This module is read-only and never touches execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

SOFIA = ZoneInfo("Europe/Sofia")
RUNNING_ACCOUNT_VALUE_START_DAY = date(2026, 9, 23)

# No day may carry a hand-entered balance. MT5/Vantage snapshots are the single source
# of truth for each Sofia trading day's opening and closing balance.
_VERIFIED_OPENING_VALUES: dict[date, Decimal] = {}


def _money(value: Decimal | str | float | int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _day_start_utc(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=SOFIA).astimezone(UTC)


def opening_account_value(
    session: Session,
    user_id: UUID,
    day: date,
) -> Decimal | None:
    fixed = _VERIFIED_OPENING_VALUES.get(day)
    if fixed is not None:
        return fixed

    row = session.execute(
        text(
            """
            SELECT pas.balance AS opening_value
            FROM performance_account_snapshots pas
            JOIN mt5_accounts a ON a.id=pas.mt5_account_id
            WHERE a.owner_user_id=:user_id
              AND a.status<>'revoked'
              AND pas.captured_at<:day_start
            ORDER BY pas.captured_at DESC
            LIMIT 1
            """
        ),
        {"user_id": user_id, "day_start": _day_start_utc(day)},
    ).mappings().first()
    if row is None:
        return None
    value = row.get("opening_value")
    if value is None:
        # Lightweight unit-test stubs return the snapshot shape rather than the SQL
        # alias; accepting equity here changes nothing in production.
        value = row.get("balance")
    if value is None:
        return None
    return _money(value)


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
    point = (now or datetime.now(UTC)).astimezone(UTC)
    local_today = point.astimezone(SOFIA).date()
    start_at = _day_start_utc(RUNNING_ACCOUNT_VALUE_START_DAY)

    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (local_day)
                local_day,balance,captured_at
            FROM (
                SELECT
                    timezone('Europe/Sofia',pas.captured_at)::date AS local_day,
                    pas.balance,
                    pas.captured_at
                FROM performance_account_snapshots pas
                JOIN mt5_accounts a ON a.id=pas.mt5_account_id
                WHERE a.owner_user_id=:user_id
                  AND a.status<>'revoked'
                  AND pas.captured_at>=:start_at
                  AND pas.captured_at<=:end_at
                  AND pas.balance IS NOT NULL
            ) snapshots
            ORDER BY local_day,captured_at DESC
            """
        ),
        {"user_id": user_id, "start_at": start_at, "end_at": point},
    ).mappings().all()

    result: list[RunningAccountDay] = []
    previous_close: Decimal | None = None
    for row in rows:
        day = row["local_day"]
        closing = _money(row["balance"])
        opening = _VERIFIED_OPENING_VALUES.get(day)
        if opening is None:
            opening = previous_close
        if opening is None:
            opening = opening_account_value(session, user_id, day)
        previous_close = closing

        if opening is None or day.weekday() >= 5 or day > local_today:
            continue
        result.append(
            RunningAccountDay(
                day=day,
                opening_value=_money(opening),
                closing_value=closing,
            )
        )
    return tuple(result)


__all__ = [
    "RUNNING_ACCOUNT_VALUE_START_DAY",
    "RunningAccountDay",
    "account_value_days",
    "opening_account_value",
]
