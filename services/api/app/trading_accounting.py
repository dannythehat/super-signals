"""Canonical trading-accounting truth shared by paper and LIVE.

Performance is always realised Super Signals trading P/L. Broker deposits, withdrawals,
manual demo resets and other DEAL_TYPE_BALANCE movements are capital movements, never
profit/loss.

The Owner demo account has one historical carry-forward because its broker balance was
manually reset during acceptance testing. From Monday 17 August 2026 onward, immutable
broker exit deals are used by their actual occurrence time in Europe/Sofia. The Owner demo
balance is therefore USD 1,000 + cumulative realised Super Signals P/L and that exact same
balance is used for risk sizing.

LIVE accounts use the identical reporting windows and daily series. Their displayed/sizing
balance remains the actual broker balance, which naturally reflects real deposits and
withdrawals while those movements remain excluded from performance.
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

TRADING_ACCOUNTING_TIMEZONE = "Europe/Sofia"
TRADING_ACCOUNTING_ZONE = ZoneInfo(TRADING_ACCOUNTING_TIMEZONE)
OWNER_DEMO_BASELINE_BALANCE = Decimal("1000.00")
OWNER_DEMO_CARRY_IN_PNL = Decimal("219.04")
OWNER_DEMO_SERIES_START_LOCAL_DATE = date(2026, 8, 17)
OWNER_DEMO_SERIES_STARTED_AT = datetime.combine(
    OWNER_DEMO_SERIES_START_LOCAL_DATE,
    time.min,
    tzinfo=TRADING_ACCOUNTING_ZONE,
).astimezone(UTC)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _local_midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=TRADING_ACCOUNTING_ZONE).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TradingProfitWindows:
    today: Decimal
    week: Decimal
    month: Decimal
    rolling_30d: Decimal
    all_time: Decimal


@dataclass(frozen=True, slots=True)
class DailyTradingProfit:
    day: date
    pnl: Decimal


class CanonicalTradingAccountingService:
    """One money ledger for dashboard reporting, charts and risk-sizing balance."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def is_owner_demo(user_id: UUID) -> bool:
        return active_paper_epoch(user_id) is not None

    def _account_ids(self, user_id: UUID) -> tuple[UUID, ...]:
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT id
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status<>'revoked'
                    ORDER BY created_at
                    """
                ),
                {"user_id": user_id},
            ).scalars().all()
        return tuple(UUID(str(value)) for value in values)

    def _first_super_signals_exit(self, user_id: UUID) -> datetime | None:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT MIN(bd.occurred_at)
                    FROM broker_deals AS bd
                    WHERE bd.user_id=:user_id
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND bd.signal_id IS NOT NULL
                    """
                ),
                {"user_id": user_id},
            ).scalar_one_or_none()
        return _utc(value) if isinstance(value, datetime) else None

    def series_start(self, user_id: UUID) -> datetime | None:
        if self.is_owner_demo(user_id):
            return OWNER_DEMO_SERIES_STARTED_AT
        return self._first_super_signals_exit(user_id)

    def _sum_exits(self, user_id: UUID, start: datetime, end: datetime) -> Decimal:
        start_utc = _utc(start)
        end_utc = _utc(end)
        if self.is_owner_demo(user_id):
            start_utc = max(start_utc, OWNER_DEMO_SERIES_STARTED_AT)
        if end_utc <= start_utc:
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
                    WHERE bd.user_id=:user_id
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND bd.signal_id IS NOT NULL
                      AND bd.occurred_at>=:start_at
                      AND bd.occurred_at<:end_at
                    """
                ),
                {"user_id": user_id, "start_at": start_utc, "end_at": end_utc},
            ).scalar_one()
        return Decimal(str(value or 0))

    def realised_between(
        self,
        user_id: UUID,
        start: datetime,
        end: datetime,
        *,
        include_owner_carry_in: bool = False,
    ) -> Decimal:
        value = self._sum_exits(user_id, start, end)
        if include_owner_carry_in and self.is_owner_demo(user_id):
            value += OWNER_DEMO_CARRY_IN_PNL
        return _money(value)

    def all_time_pnl(self, user_id: UUID, *, now: datetime | None = None) -> Decimal:
        point = _utc(now or datetime.now(UTC))
        start = self.series_start(user_id)
        if start is None:
            return Decimal("0.00")
        return self.realised_between(
            user_id,
            start,
            point,
            include_owner_carry_in=self.is_owner_demo(user_id),
        )

    def displayed_balance(
        self,
        user_id: UUID,
        *,
        broker_balance: Decimal | float | str,
        now: datetime | None = None,
    ) -> Decimal:
        if not self.is_owner_demo(user_id):
            return _money(Decimal(str(broker_balance)))
        return _money(OWNER_DEMO_BASELINE_BALANCE + self.all_time_pnl(user_id, now=now))

    def windows(self, user_id: UUID, *, now: datetime | None = None) -> TradingProfitWindows:
        point = _utc(now or datetime.now(UTC))
        local_now = point.astimezone(TRADING_ACCOUNTING_ZONE)
        today_start = _local_midnight(local_now.date())
        week_start = _local_midnight(local_now.date() - timedelta(days=local_now.weekday()))
        month_start = _local_midnight(local_now.date().replace(day=1))
        rolling_30d_start = point - timedelta(days=30)
        start = self.series_start(user_id)
        if start is None:
            zero = Decimal("0.00")
            return TradingProfitWindows(zero, zero, zero, zero, zero)

        def period(start_at: datetime) -> Decimal:
            include_carry = self.is_owner_demo(user_id) and start_at <= OWNER_DEMO_SERIES_STARTED_AT
            return self.realised_between(
                user_id,
                max(start_at, start),
                point,
                include_owner_carry_in=include_carry,
            )

        return TradingProfitWindows(
            today=period(today_start),
            week=period(week_start),
            month=period(month_start),
            rolling_30d=period(rolling_30d_start),
            all_time=self.all_time_pnl(user_id, now=point),
        )

    def daily(self, user_id: UUID, *, now: datetime | None = None) -> tuple[DailyTradingProfit, ...]:
        point = _utc(now or datetime.now(UTC))
        start = self.series_start(user_id)
        if start is None:
            return ()
        start_local = start.astimezone(TRADING_ACCOUNTING_ZONE).date()
        end_local = point.astimezone(TRADING_ACCOUNTING_ZONE).date()
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
                    WHERE bd.user_id=:user_id
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND bd.signal_id IS NOT NULL
                      AND bd.occurred_at>=:start_at
                      AND bd.occurred_at<:end_at
                    GROUP BY 1
                    ORDER BY 1
                    """
                ),
                {"user_id": user_id, "start_at": start, "end_at": point},
            ).mappings().all()
        by_day = {row["local_day"]: _money(Decimal(str(row["pnl"] or 0))) for row in rows}
        values: list[DailyTradingProfit] = []
        cursor = start_local
        while cursor <= end_local:
            values.append(DailyTradingProfit(day=cursor, pnl=by_day.get(cursor, Decimal("0.00"))))
            cursor += timedelta(days=1)
        return tuple(values)


__all__ = [
    "CanonicalTradingAccountingService",
    "DailyTradingProfit",
    "OWNER_DEMO_BASELINE_BALANCE",
    "OWNER_DEMO_CARRY_IN_PNL",
    "OWNER_DEMO_SERIES_STARTED_AT",
    "TRADING_ACCOUNTING_TIMEZONE",
    "TradingProfitWindows",
]
