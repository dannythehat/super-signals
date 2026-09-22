"""Telegram-only presentation for the main Super Signals reference account.

This module is read-only. It does not alter broker execution, subscriber balances,
provider risk, or AIDY authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.reporting_overrides import BROKER_DEAL_NOT_OVERRIDDEN_SQL, override_cash_by_day

SOFIA = ZoneInfo("Europe/Sofia")
REFERENCE_START_AT = datetime(2026, 8, 5, 21, 0, tzinfo=UTC)
REFERENCE_START_LABEL = "6 Aug 2026"
REFERENCE_START_VALUE = Decimal("1000.00")

# A published account figure must never look current when the MetaAPI/Vantage
# capture loop has stalled. Tunable without a deploy.
ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS = max(
    60, int(os.getenv("TELEGRAM_ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS", "900") or 900)
)
_PROVIDER_COLOURS = ("🔵", "🟢", "🟣", "🟠", "🟡", "🔴", "🟤", "⚫", "⚪")


def money(value: Decimal | int | float | str | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    return f"{sign}$" + f"{abs(amount):,.2f}"


def provider_badge(source_id: UUID | str | None) -> str:
    raw = str(source_id or "unknown").encode("utf-8")
    digest = sha256(raw).digest()
    first = digest[0] % len(_PROVIDER_COLOURS)
    second = digest[1] % (len(_PROVIDER_COLOURS) - 1)
    if second >= first:
        second += 1
    return _PROVIDER_COLOURS[first] + _PROVIDER_COLOURS[second]


@dataclass(frozen=True, slots=True)
class AccountLedgerSnapshot:
    account_value: Decimal | None
    mt5_balance: Decimal | None
    today_pnl: Decimal
    month_to_date_pnl: Decimal
    one_percent: Decimal | None
    local_weekday: int
    updated_at: datetime | None
    stale: bool = False

    @property
    def weekday_trading_day(self) -> bool:
        return self.local_weekday < 5


@dataclass(frozen=True, slots=True)
class TradeLedgerSnapshot:
    realised_pnl: Decimal
    total_legs: int
    closed_legs: int
    open_legs: int
    pending_legs: int

    @property
    def complete(self) -> bool:
        return self.total_legs > 0 and self.open_legs == 0 and self.pending_legs == 0


class TelegramTradeLedger:
    """Read-only main-account ledger for member-facing Telegram posts."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        reference_user_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._reference_user_id = reference_user_id

    def account(self, *, now: datetime | None = None) -> AccountLedgerSnapshot:
        point = (now or datetime.now(UTC)).astimezone(UTC)
        local_point = point.astimezone(SOFIA)
        local_day = local_point.date()
        month_start = local_day.replace(day=1)

        with self._session_factory() as session:
            snapshot = session.execute(
                text(
                    """
                    SELECT pas.balance,pas.equity,pas.captured_at
                    FROM performance_account_snapshots pas
                    JOIN mt5_accounts a ON a.id=pas.mt5_account_id
                    WHERE a.owner_user_id=:user_id
                      AND a.status<>'revoked'
                    ORDER BY pas.captured_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": self._reference_user_id},
            ).mappings().first()

            rows = session.execute(
                text(
                    f"""
                    SELECT
                        timezone('Europe/Sofia',bd.occurred_at)::date AS day,
                        COALESCE(SUM(
                            COALESCE(bd.profit,0)
                            + COALESCE(bd.commission,0)
                            + COALESCE(bd.swap,0)
                        ),0) AS pnl
                    FROM broker_deals bd
                    JOIN sources src ON src.id=bd.source_id
                    WHERE bd.user_id=:user_id
                      AND src.status<>'revoked'
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND (bd.signal_id IS NOT NULL OR bd.broker_client_id LIKE 'SS_%')
                      AND bd.occurred_at>=:start_at
                      AND bd.occurred_at<:end_at
                      AND {BROKER_DEAL_NOT_OVERRIDDEN_SQL}
                    GROUP BY 1
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "start_at": REFERENCE_START_AT,
                    "end_at": point,
                },
            ).mappings().all()

            reviewed = session.execute(
                text(
                    """
                    SELECT
                        timezone('Europe/Sofia',pto.closed_at)::date AS day,
                        COALESCE(SUM(pto.cash_pnl),0) AS pnl
                    FROM performance_trade_outcomes pto
                    WHERE pto.user_id=:user_id
                      AND pto.closed_at>=:start_at
                      AND pto.closed_at<:end_at
                      AND pto.broker_deal_count=0
                      AND pto.close_reason LIKE 'reviewed_provider_%'
                    GROUP BY 1
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "start_at": REFERENCE_START_AT,
                    "end_at": point,
                },
            ).mappings().all()

            overrides = override_cash_by_day(
                session,
                self._reference_user_id,
                start=REFERENCE_START_AT,
                end=point,
            )

        by_day: dict[object, Decimal] = {}
        for row in rows:
            day = row["day"]
            by_day[day] = by_day.get(day, Decimal("0")) + Decimal(str(row["pnl"] or 0))
        for row in reviewed:
            day = row["day"]
            by_day[day] = by_day.get(day, Decimal("0")) + Decimal(str(row["pnl"] or 0))
        for day, amount in overrides.items():
            by_day[day] = by_day.get(day, Decimal("0")) + Decimal(str(amount or 0))

        today = by_day.get(local_day, Decimal("0")).quantize(Decimal("0.01"))
        month = sum(
            (
                amount
                for day, amount in by_day.items()
                if hasattr(day, "year")
                and day >= month_start
                and day <= local_day
            ),
            Decimal("0"),
        ).quantize(Decimal("0.01"))

        account_value = (
            Decimal(str(snapshot["equity"])).quantize(Decimal("0.01"))
            if snapshot is not None and snapshot["equity"] is not None
            else None
        )
        mt5_balance = (
            Decimal(str(snapshot["balance"])).quantize(Decimal("0.01"))
            if snapshot is not None and snapshot["balance"] is not None
            else None
        )
        captured_at = snapshot["captured_at"] if snapshot is not None else None
        stale = True
        if captured_at is not None:
            captured = captured_at
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=UTC)
            age = (point - captured.astimezone(UTC)).total_seconds()
            stale = age > ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS

        # Risk basis parity: execution sizes every trade off the broker BALANCE
        # (mt5_execution_day26.py risk_balance = live_state.account.balance, then
        # risk_sizing_day24.py risk_budget = balance * risk_percent / 100).
        # Publishing 1% of EQUITY would advertise a figure that differs from the
        # risk actually taken whenever a position is open. A stale snapshot must
        # not be quoted as a current risk figure at all.
        one_percent = (
            (mt5_balance * Decimal("0.01")).quantize(Decimal("0.01"))
            if mt5_balance is not None and not stale
            else None
        )

        return AccountLedgerSnapshot(
            account_value=account_value,
            mt5_balance=mt5_balance,
            today_pnl=today,
            month_to_date_pnl=month,
            one_percent=one_percent,
            local_weekday=local_point.weekday(),
            updated_at=captured_at,
            stale=stale,
        )

    def trade(self, signal_id: UUID) -> TradeLedgerSnapshot:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        COUNT(p.id)::int AS total_legs,
                        COUNT(p.id) FILTER (
                            WHERE COALESCE(o.status,'') IN
                                ('won','lost','breakeven','closed_unknown')
                               OR (
                                  p.status='closed'
                                  AND COALESCE(o.status,'') NOT IN ('open','pending')
                               )
                        )::int AS closed_legs,
                        COUNT(p.id) FILTER (
                            WHERE p.status='open'
                              AND p.broker_position_id IS NOT NULL
                              AND COALESCE(o.status,'open') NOT IN
                                  ('won','lost','breakeven','closed_unknown')
                        )::int AS open_legs,
                        COUNT(p.id) FILTER (
                            WHERE p.status IN ('planned','pending')
                              AND COALESCE(p.broker_order_id,p.broker_position_id) IS NOT NULL
                              AND COALESCE(o.status,'pending') NOT IN
                                  ('won','lost','breakeven','closed_unknown')
                        )::int AS pending_legs,
                        COALESCE(SUM(
                            CASE
                                WHEN o.status IN ('won','lost','breakeven','closed_unknown')
                                THEN COALESCE(o.cash_pnl,0)
                                ELSE 0
                            END
                        ),0) AS realised_pnl
                    FROM positions p
                    LEFT JOIN performance_trade_outcomes o ON o.position_id=p.id
                    WHERE p.signal_id=:signal_id
                      AND p.user_id=:user_id
                    """
                ),
                {"signal_id": signal_id, "user_id": self._reference_user_id},
            ).mappings().one()
        return TradeLedgerSnapshot(
            realised_pnl=Decimal(str(row["realised_pnl"] or 0)),
            total_legs=int(row["total_legs"] or 0),
            closed_legs=int(row["closed_legs"] or 0),
            open_legs=int(row["open_legs"] or 0),
            pending_legs=int(row["pending_legs"] or 0),
        )

    @staticmethod
    def account_lines(
        snapshot: AccountLedgerSnapshot,
        *,
        include_origin: bool = False,
    ) -> list[str]:
        lines: list[str] = []
        if snapshot.weekday_trading_day:
            lines.append(f"📅 Today: {money(snapshot.today_pnl)}")
        lines.append(f"📆 Month to date: {money(snapshot.month_to_date_pnl)}")
        if snapshot.account_value is not None:
            value = "$" + f"{snapshot.account_value:,.2f}"
            # 1% is quoted from BALANCE because that is what execution sizes from,
            # and only when the capture is fresh (see account()).
            one_percent = (
                " · 1% = $" + f"{snapshot.one_percent:,.2f}"
                if snapshot.one_percent is not None
                else ""
            )
            if snapshot.stale:
                stamp = (
                    snapshot.updated_at.astimezone(SOFIA).strftime("%H:%M")
                    if snapshot.updated_at is not None
                    else "unknown"
                )
                lines.append(
                    f"💰 Vantage account value: {value} (last updated {stamp})"
                )
            else:
                lines.append(f"💰 Vantage account value: {value}{one_percent}")
        if include_origin:
            lines.append(
                "🏁 Started $" + f"{REFERENCE_START_VALUE:,.2f}" + f" · {REFERENCE_START_LABEL}"
            )
        return lines


__all__ = [
    "AccountLedgerSnapshot",
    "REFERENCE_START_AT",
    "REFERENCE_START_LABEL",
    "REFERENCE_START_VALUE",
    "TelegramTradeLedger",
    "TradeLedgerSnapshot",
    "money",
    "provider_badge",
]
