"""Canonical trading-accounting truth shared by paper and LIVE.

Performance is realised Super Signals trading P/L. Deposits, withdrawals, manual demo
resets and other broker balance movements are capital movements, never profit/loss.

Each account is isolated. Dashboard windows and daily history use the user's detected IANA
timezone, while immutable broker-deal timestamps remain stored in UTC. A daily return is
that day's realised Super Signals P/L divided by the account balance at the start of that
local calendar day.

The Owner demo uses the active paper epoch as its synthetic capital origin. For the
current clean run that is USD 1,500 from 27 August 2026 11:30 Europe/Sofia, with no
historical carry-in. That exact synthetic balance is also used for percentage risk sizing.
LIVE accounts use their actual broker balance; deposits/withdrawals affect capital but
never performance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.paper_run_epoch import (
    PAPER_RUN_BASELINE_BALANCE,
    PAPER_RUN_STARTED_AT,
    active_paper_epoch,
)

DEFAULT_TRADING_TIMEZONE = "UTC"
OWNER_DEMO_ACCOUNTING_ZONE = ZoneInfo("Europe/Sofia")
OWNER_DEMO_BASELINE_BALANCE = PAPER_RUN_BASELINE_BALANCE
OWNER_DEMO_CARRY_IN_PNL = Decimal("0.00")
OWNER_DEMO_SERIES_STARTED_AT = PAPER_RUN_STARTED_AT


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _percent(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def resolve_timezone(timezone_name: str | None) -> tuple[str, ZoneInfo]:
    requested = (timezone_name or DEFAULT_TRADING_TIMEZONE).strip() or DEFAULT_TRADING_TIMEZONE
    try:
        return requested, ZoneInfo(requested)
    except ZoneInfoNotFoundError:
        return DEFAULT_TRADING_TIMEZONE, ZoneInfo(DEFAULT_TRADING_TIMEZONE)


def _local_midnight(value: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(value, time.min, tzinfo=zone).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TradingProfitWindows:
    timezone: str
    today: Decimal
    week: Decimal
    month: Decimal
    rolling_30d: Decimal
    all_time: Decimal


@dataclass(frozen=True, slots=True)
class DailyTradingProfit:
    day: date
    pnl: Decimal
    opening_balance: Decimal
    return_percent: Decimal


class CanonicalTradingAccountingService:
    """One money ledger for dashboard reporting, charts and risk-sizing balance."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def _active_account(self, user_id: UUID) -> tuple[UUID, str] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id,account_environment
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status<>'revoked'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return None
        return UUID(str(row["id"])), str(row["account_environment"] or "").strip().lower()

    def uses_synthetic_demo_balance(self, user_id: UUID) -> bool:
        account = self._active_account(user_id)
        return bool(
            account is not None
            and account[1] == "demo"
            and active_paper_epoch(user_id) is not None
        )

    def _first_super_signals_exit(self, user_id: UUID, account_id: UUID) -> datetime | None:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT MIN(bd.occurred_at)
                    FROM broker_deals AS bd
                    WHERE bd.user_id=:user_id
                      AND bd.mt5_account_id=:account_id
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND (bd.signal_id IS NOT NULL OR bd.broker_client_id LIKE 'SS_%')
                    """
                ),
                {"user_id": user_id, "account_id": account_id},
            ).scalar_one_or_none()
        return _utc(value) if isinstance(value, datetime) else None

    def series_start(self, user_id: UUID) -> datetime | None:
        account = self._active_account(user_id)
        if account is None:
            return None
        account_id, _ = account
        if self.uses_synthetic_demo_balance(user_id):
            return OWNER_DEMO_SERIES_STARTED_AT
        return self._first_super_signals_exit(user_id, account_id)

    def _sum_exits(self, user_id: UUID, start: datetime, end: datetime) -> Decimal:
        account = self._active_account(user_id)
        if account is None:
            return Decimal("0")
        account_id, _ = account
        start_utc = _utc(start)
        end_utc = _utc(end)
        if self.uses_synthetic_demo_balance(user_id):
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
                      AND bd.mt5_account_id=:account_id
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND (bd.signal_id IS NOT NULL OR bd.broker_client_id LIKE 'SS_%')
                      AND bd.occurred_at>=:start_at
                      AND bd.occurred_at<:end_at
                    """
                ),
                {
                    "user_id": user_id,
                    "account_id": account_id,
                    "start_at": start_utc,
                    "end_at": end_utc,
                },
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
        if include_owner_carry_in and self.uses_synthetic_demo_balance(user_id):
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
            include_owner_carry_in=self.uses_synthetic_demo_balance(user_id),
        )

    def displayed_balance(
        self,
        user_id: UUID,
        *,
        broker_balance: Decimal | float | str,
        now: datetime | None = None,
    ) -> Decimal:
        if not self.uses_synthetic_demo_balance(user_id):
            return _money(Decimal(str(broker_balance)))
        return _money(OWNER_DEMO_BASELINE_BALANCE + self.all_time_pnl(user_id, now=now))

    def windows(
        self,
        user_id: UUID,
        *,
        timezone_name: str | None,
        now: datetime | None = None,
    ) -> TradingProfitWindows:
        point = _utc(now or datetime.now(UTC))
        resolved_timezone, zone = resolve_timezone(timezone_name)
        local_now = point.astimezone(zone)
        today_start = _local_midnight(local_now.date(), zone)
        week_start = _local_midnight(local_now.date() - timedelta(days=local_now.weekday()), zone)
        month_start = _local_midnight(local_now.date().replace(day=1), zone)
        rolling_30d_start = point - timedelta(days=30)
        start = self.series_start(user_id)
        if start is None:
            zero = Decimal("0.00")
            return TradingProfitWindows(resolved_timezone, zero, zero, zero, zero, zero)

        synthetic = self.uses_synthetic_demo_balance(user_id)

        def period(start_at: datetime) -> Decimal:
            include_carry = synthetic and start_at < OWNER_DEMO_SERIES_STARTED_AT
            return self.realised_between(
                user_id,
                max(start_at, start),
                point,
                include_owner_carry_in=include_carry,
            )

        return TradingProfitWindows(
            timezone=resolved_timezone,
            today=period(today_start),
            week=period(week_start),
            month=period(month_start),
            rolling_30d=period(rolling_30d_start),
            all_time=self.all_time_pnl(user_id, now=point),
        )

    def _daily_super_signals_pnl(
        self,
        user_id: UUID,
        account_id: UUID,
        *,
        start: datetime,
        end: datetime,
        timezone_name: str,
    ) -> dict[date, Decimal]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        timezone(:timezone_name, bd.occurred_at)::date AS local_day,
                        COALESCE(SUM(
                            COALESCE(bd.profit,0)
                            + COALESCE(bd.commission,0)
                            + COALESCE(bd.swap,0)
                        ),0) AS pnl
                    FROM broker_deals AS bd
                    WHERE bd.user_id=:user_id
                      AND bd.mt5_account_id=:account_id
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND (bd.signal_id IS NOT NULL OR bd.broker_client_id LIKE 'SS_%')
                      AND bd.occurred_at>=:start_at
                      AND bd.occurred_at<:end_at
                    GROUP BY 1
                    ORDER BY 1
                    """
                ),
                {
                    "user_id": user_id,
                    "account_id": account_id,
                    "timezone_name": timezone_name,
                    "start_at": start,
                    "end_at": end,
                },
            ).mappings().all()
        return {
            row["local_day"]: _money(Decimal(str(row["pnl"] or 0)))
            for row in rows
        }

    def _daily_total_balance_changes(
        self,
        user_id: UUID,
        account_id: UUID,
        *,
        start: datetime,
        end: datetime,
        timezone_name: str,
    ) -> dict[date, Decimal]:
        """All broker balance-changing deal values, including capital movements."""
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        timezone(:timezone_name, bd.occurred_at)::date AS local_day,
                        COALESCE(SUM(
                            COALESCE(bd.profit,0)
                            + COALESCE(bd.commission,0)
                            + COALESCE(bd.swap,0)
                        ),0) AS balance_change
                    FROM broker_deals AS bd
                    WHERE bd.user_id=:user_id
                      AND bd.mt5_account_id=:account_id
                      AND bd.occurred_at>=:start_at
                      AND bd.occurred_at<:end_at
                    GROUP BY 1
                    ORDER BY 1
                    """
                ),
                {
                    "user_id": user_id,
                    "account_id": account_id,
                    "timezone_name": timezone_name,
                    "start_at": start,
                    "end_at": end,
                },
            ).mappings().all()
        return {
            row["local_day"]: _money(Decimal(str(row["balance_change"] or 0)))
            for row in rows
        }

    def daily(
        self,
        user_id: UUID,
        *,
        broker_balance: Decimal | float | str,
        timezone_name: str | None,
        now: datetime | None = None,
    ) -> tuple[DailyTradingProfit, ...]:
        point = _utc(now or datetime.now(UTC))
        account = self._active_account(user_id)
        start = self.series_start(user_id)
        if account is None or start is None:
            return ()
        account_id, _ = account
        resolved_timezone, zone = resolve_timezone(timezone_name)
        start_local = start.astimezone(zone).date()
        end_local = point.astimezone(zone).date()
        history_start = _local_midnight(start_local, zone)
        by_day = self._daily_super_signals_pnl(
            user_id,
            account_id,
            start=start,
            end=point,
            timezone_name=resolved_timezone,
        )

        opening_by_day: dict[date, Decimal] = {}
        if self.uses_synthetic_demo_balance(user_id):
            running = _money(OWNER_DEMO_BASELINE_BALANCE + OWNER_DEMO_CARRY_IN_PNL)
            cursor = start_local
            while cursor <= end_local:
                opening_by_day[cursor] = running
                running = _money(running + by_day.get(cursor, Decimal("0.00")))
                cursor += timedelta(days=1)
        else:
            changes = self._daily_total_balance_changes(
                user_id,
                account_id,
                start=history_start,
                end=point,
                timezone_name=resolved_timezone,
            )
            running_end = _money(Decimal(str(broker_balance)))
            cursor = end_local
            while cursor >= start_local:
                opening = _money(running_end - changes.get(cursor, Decimal("0.00")))
                opening_by_day[cursor] = opening
                running_end = opening
                cursor -= timedelta(days=1)

        values: list[DailyTradingProfit] = []
        cursor = start_local
        while cursor <= end_local:
            pnl = by_day.get(cursor, Decimal("0.00"))
            opening = opening_by_day.get(cursor, Decimal("0.00"))
            return_percent = (
                _percent(pnl / opening * Decimal("100"))
                if opening != 0
                else Decimal("0.00")
            )
            values.append(
                DailyTradingProfit(
                    day=cursor,
                    pnl=pnl,
                    opening_balance=opening,
                    return_percent=return_percent,
                )
            )
            cursor += timedelta(days=1)
        return tuple(values)


__all__ = [
    "CanonicalTradingAccountingService",
    "DailyTradingProfit",
    "DEFAULT_TRADING_TIMEZONE",
    "OWNER_DEMO_BASELINE_BALANCE",
    "OWNER_DEMO_CARRY_IN_PNL",
    "OWNER_DEMO_SERIES_STARTED_AT",
    "TradingProfitWindows",
    "resolve_timezone",
]
