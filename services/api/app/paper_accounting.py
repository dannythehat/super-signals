"""Canonical Owner paper-accounting truth.

The demo broker balance was manually reset during acceptance testing, so raw MT5 balance
cannot be used as the product's cumulative paper balance.  Super Signals therefore keeps
one explicit carried-in realised P/L checkpoint and then accumulates immutable broker exit
deals from that point forward.  Balance-reset DEAL_TYPE_BALANCE rows are never trading P/L.

The carried-in amount is the accepted cumulative result before Monday 17 August 2026.
From that Monday onward every day is reconstructed from broker deal occurrence time in
Europe/Sofia.  This same balance is used by the Owner demo dashboard and by 1%-per-leg
risk sizing, so reporting and execution cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.paper_run_epoch import active_paper_epoch

PAPER_ACCOUNTING_TIMEZONE = "Europe/Sofia"
PAPER_ACCOUNTING_ZONE = ZoneInfo(PAPER_ACCOUNTING_TIMEZONE)
PAPER_ACCOUNTING_BASELINE_BALANCE = Decimal("1000.00")
PAPER_ACCOUNTING_CARRY_IN_PNL = Decimal("219.04")
PAPER_ACCOUNTING_START_LOCAL_DATE = date(2026, 8, 17)
PAPER_ACCOUNTING_STARTED_AT = datetime.combine(
    PAPER_ACCOUNTING_START_LOCAL_DATE,
    time.min,
    tzinfo=PAPER_ACCOUNTING_ZONE,
).astimezone(UTC)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _local_midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=PAPER_ACCOUNTING_ZONE).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PaperProfitWindows:
    today: Decimal
    week: Decimal
    month: Decimal
    all_time: Decimal
    balance: Decimal


@dataclass(frozen=True, slots=True)
class DailyPaperProfit:
    day: date
    pnl: Decimal


class PaperAccountingService:
    """Read-only cumulative paper balance and period P/L over broker deal truth."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def applies(user_id: UUID) -> bool:
        return active_paper_epoch(user_id) is not None

    def _sum_exits(self, user_id: UUID, start: datetime, end: datetime) -> Decimal:
        start = max(_utc(start), PAPER_ACCOUNTING_STARTED_AT)
        end = _utc(end)
        if end <= start:
            return Decimal("0")
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(
                        COALESCE(bd.profit,0)
                        + COALESCE(bd.commission,0)
                        + COALESCE(bd.swap,0)
                    ),0)
                    FROM broker_deals AS bd
                    WHERE bd.mt5_account_id IN (
                        SELECT id FROM mt5_accounts
                        WHERE owner_user_id=:user_id AND status<>'revoked'
                    )
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND bd.occurred_at>=:start_at
                      AND bd.occurred_at<:end_at
                    """
                ),
                {"user_id": user_id, "start_at": start, "end_at": end},
            ).scalar_one()
        return Decimal(str(value or 0))

    def realised_between(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        *,
        include_carry_in: bool = False,
    ) -> Decimal:
        value = self._sum_exits(user_id, start, end)
        if include_carry_in:
            value += PAPER_ACCOUNTING_CARRY_IN_PNL
        return _money(value)

    def all_time_pnl(self, user_id: UUID, *, now: datetime | None = None) -> Decimal:
        point = _utc(now or datetime.now(UTC))
        return self.realised_between(
            user_id,
            PAPER_ACCOUNTING_STARTED_AT,
            point,
            include_carry_in=True,
        )

    def balance(self, user_id: UUID, *, now: datetime | None = None) -> Decimal:
        return _money(PAPER_ACCOUNTING_BASELINE_BALANCE + self.all_time_pnl(user_id, now=now))

    def windows(self, user_id: UUID, *, now: datetime | None = None) -> PaperProfitWindows:
        point = _utc(now or datetime.now(UTC))
        local_now = point.astimezone(PAPER_ACCOUNTING_ZONE)
        today_start = _local_midnight(local_now.date())
        week_start_date = local_now.date() - timedelta(days=local_now.weekday())
        week_start = _local_midnight(week_start_date)
        month_start = _local_midnight(local_now.date().replace(day=1))

        today = self.realised_between(user_id, today_start, point)
        week = self.realised_between(user_id, week_start, point)
        month = self.realised_between(
            user_id,
            month_start,
            point,
            include_carry_in=month_start < PAPER_ACCOUNTING_STARTED_AT,
        )
        all_time = self.all_time_pnl(user_id, now=point)
        return PaperProfitWindows(
            today=today,
            week=week,
            month=month,
            all_time=all_time,
            balance=_money(PAPER_ACCOUNTING_BASELINE_BALANCE + all_time),
        )

    def daily(self, user_id: UUID, *, now: datetime | None = None) -> tuple[DailyPaperProfit, ...]:
        point = _utc(now or datetime.now(UTC))
        local_end = point.astimezone(PAPER_ACCOUNTING_ZONE).date()
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        (bd.occurred_at AT TIME ZONE 'Europe/Sofia')::date AS local_day,
                        COALESCE(SUM(
                            COALESCE(bd.profit,0)
                            + COALESCE(bd.commission,0)
                            + COALESCE(bd.swap,0)
                        ),0) AS pnl
                    FROM broker_deals AS bd
                    WHERE bd.mt5_account_id IN (
                        SELECT id FROM mt5_accounts
                        WHERE owner_user_id=:user_id AND status<>'revoked'
                    )
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND bd.occurred_at>=:start_at
                      AND bd.occurred_at<:end_at
                    GROUP BY 1
                    ORDER BY 1
                    """
                ),
                {
                    "user_id": user_id,
                    "start_at": PAPER_ACCOUNTING_STARTED_AT,
                    "end_at": point,
                },
            ).mappings().all()
        by_day = {row["local_day"]: _money(Decimal(str(row["pnl"] or 0))) for row in rows}
        values: list[DailyPaperProfit] = []
        cursor = PAPER_ACCOUNTING_START_LOCAL_DATE
        while cursor <= local_end:
            values.append(DailyPaperProfit(day=cursor, pnl=by_day.get(cursor, Decimal("0.00"))))
            cursor += timedelta(days=1)
        return tuple(values)


__all__ = [
    "DailyPaperProfit",
    "PAPER_ACCOUNTING_BASELINE_BALANCE",
    "PAPER_ACCOUNTING_CARRY_IN_PNL",
    "PAPER_ACCOUNTING_STARTED_AT",
    "PAPER_ACCOUNTING_TIMEZONE",
    "PaperAccountingService",
    "PaperProfitWindows",
]
