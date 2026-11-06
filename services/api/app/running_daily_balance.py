"""Shared 21:00-Sofia equity calendar for website/member reporting.

The owner-facing account total is Vantage/MT5 equity: closed balance plus floating P/L.
Each reported day spans 21:00 Europe/Sofia to the next 21:00. Trading cash and
floating movement are separated from deposits, withdrawals and credit; raw account
values remain unchanged and any ledger gap remains visible. The current reported
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

from app.performance_ledger_day33 import BROKER_FEE_SQL, TRADING_DEAL_TYPES_SQL

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
    realised_pnl: Decimal = Decimal("0")
    floating_pnl_change: Decimal = Decimal("0")
    cash_movement: Decimal = Decimal("0")
    credit_movement: Decimal = Decimal("0")
    reconciliation_gap: Decimal = Decimal("0")
    currency: str | None = None
    charges_complete: bool = False

    @property
    def pnl(self) -> Decimal:
        return _money(self.realised_pnl + self.floating_pnl_change)


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
            f"""
            WITH account_scope AS (
                SELECT CASE WHEN COUNT(DISTINCT mt5_account_id)=1 AND COUNT(DISTINCT currency)=1
                       THEN MIN(mt5_account_id::text)::uuid END AS account_id
                FROM performance_account_snapshots WHERE user_id=:user_id
            ), days AS (
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
                b.day, scope.account_id AS scope_account_id,
                opening.equity AS opening_value,
                closing.equity AS closing_value,
                closing.currency,
                COALESCE(cash.missing_fees,0)=0 AS charges_complete,
                COALESCE(cash.trading,0) AS realised_pnl,
                (closing.equity-closing.balance)-(opening.equity-opening.balance)-COALESCE(cash.credit,0) AS floating_pnl_change,
                COALESCE(cash.capital,0) AS cash_movement,
                COALESCE(cash.credit,0) AS credit_movement,
                closing.balance-opening.balance-COALESCE(cash.all_cash,0) AS reconciliation_gap
            FROM boundaries b CROSS JOIN account_scope scope
            LEFT JOIN LATERAL (
                SELECT pas.equity,pas.balance,pas.currency,pas.captured_at
                FROM performance_account_snapshots pas
                WHERE pas.user_id=:user_id AND pas.mt5_account_id=scope.account_id
                  AND pas.captured_at<=b.opening_at
                  AND pas.equity IS NOT NULL
                ORDER BY pas.captured_at DESC
                LIMIT 1
            ) opening ON TRUE
            LEFT JOIN LATERAL (
                SELECT pas.equity,pas.balance,pas.currency,pas.captured_at
                FROM performance_account_snapshots pas
                WHERE pas.user_id=:user_id AND pas.mt5_account_id=scope.account_id
                  AND pas.captured_at<=b.closing_at
                  AND pas.equity IS NOT NULL
                ORDER BY pas.captured_at DESC
                LIMIT 1
            ) closing ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    SUM(cash) FILTER (WHERE deal_type IN ({TRADING_DEAL_TYPES_SQL})) AS trading,
                    SUM(cash) FILTER (WHERE deal_type='DEAL_TYPE_BALANCE') AS capital,
                    SUM(cash) FILTER (WHERE deal_type='DEAL_TYPE_CREDIT') AS credit,
                    SUM(cash) FILTER (WHERE deal_type<>'DEAL_TYPE_CREDIT') AS all_cash,
                    COUNT(*) FILTER (WHERE fee_known IS NOT TRUE) AS missing_fees
                FROM (
                    SELECT deal_type,(raw_payload ? 'fee' AND raw_payload->>'fee' IS NOT NULL AND
                       (jsonb_typeof(raw_payload->'fee')='number' OR
                        (raw_payload->>'fee') ~ '^[+-]?[0-9]+([.][0-9]+)?([eE][+-]?[0-9]+)?$')) AS fee_known,
                       COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)+({BROKER_FEE_SQL}) AS cash
                    FROM broker_deals WHERE user_id=:user_id AND mt5_account_id=scope.account_id
                      AND occurred_at>opening.captured_at AND occurred_at<=closing.captured_at
                ) posted
            ) cash ON TRUE
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

    if rows and rows[0].get("scope_account_id") is None:
        raise ValueError("account_currency_scope_unavailable")
    return tuple(
        RunningAccountDay(
            day=row["day"],
            opening_value=_money(row["opening_value"]),
            closing_value=_money(row["closing_value"]),
            realised_pnl=_money(row.get("realised_pnl") or 0),
            floating_pnl_change=_money(row.get("floating_pnl_change") or 0),
            cash_movement=_money(row.get("cash_movement") or 0),
            credit_movement=_money(row.get("credit_movement") or 0),
            reconciliation_gap=_money(row.get("reconciliation_gap") or 0),
            currency=row.get("currency"),
            charges_complete=bool(row.get("charges_complete")),
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
