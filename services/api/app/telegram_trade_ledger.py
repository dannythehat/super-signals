"""Member-facing Super Signals ledger helpers for Telegram presentation.

The feed uses the same canonical accounting service as the dashboard and risk sizer.
Provider presentation is public by owner choice, but account/user identifiers remain private.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.trading_accounting import CanonicalTradingAccountingService

SOFIA = ZoneInfo("Europe/Sofia")
REFERENCE_START_LABEL = "6 Aug 2026"
REFERENCE_START_BALANCE = Decimal("1000.00")
_PROVIDER_COLOURS = ("🔵", "🟢", "🟣", "🟠", "🟡", "🔴", "🟤", "⚫", "⚪")


def money(value: Decimal | int | float | str | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    return f"{sign}$" + f"{abs(amount):,.2f}"


def provider_badge(source_id: UUID | str | None) -> str:
    """Stable two-colour provider marker; 72 ordered combinations before reuse."""
    raw = str(source_id or "unknown").encode("utf-8")
    digest = sha256(raw).digest()
    first = digest[0] % len(_PROVIDER_COLOURS)
    second = digest[1] % (len(_PROVIDER_COLOURS) - 1)
    if second >= first:
        second += 1
    return _PROVIDER_COLOURS[first] + _PROVIDER_COLOURS[second]


@dataclass(frozen=True, slots=True)
class AccountLedgerSnapshot:
    balance: Decimal
    today_pnl: Decimal
    month_to_date_pnl: Decimal
    one_percent: Decimal
    local_weekday: int

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
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        reference_user_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._reference_user_id = reference_user_id
        self._accounting = CanonicalTradingAccountingService(session_factory)

    def account(self, *, now: datetime | None = None) -> AccountLedgerSnapshot:
        point = (now or datetime.now(UTC)).astimezone(UTC)
        with self._session_factory() as session:
            broker_balance = session.execute(
                text(
                    """
                    SELECT last_confirmed_balance
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id
                      AND status<>'revoked'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": self._reference_user_id},
            ).scalar_one_or_none()
        balance = self._accounting.displayed_balance(
            self._reference_user_id,
            broker_balance=Decimal(str(broker_balance or 0)),
            now=point,
        )
        windows = self._accounting.windows(
            self._reference_user_id,
            timezone_name="Europe/Sofia",
            now=point,
        )
        return AccountLedgerSnapshot(
            balance=balance,
            today_pnl=windows.today,
            month_to_date_pnl=windows.month,
            one_percent=(balance * Decimal("0.01")).quantize(Decimal("0.01")),
            local_weekday=point.astimezone(SOFIA).weekday(),
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
    def account_lines(snapshot: AccountLedgerSnapshot, *, include_origin: bool = False) -> list[str]:
        lines: list[str] = []
        if snapshot.weekday_trading_day:
            lines.append(f"📅 Today: {money(snapshot.today_pnl)}")
        lines.append(f"📆 Month to date: {money(snapshot.month_to_date_pnl)}")
        lines.append(
            "💰 Super Signals balance: $"
            + f"{snapshot.balance:,.2f}"
            + " · 1% = $"
            + f"{snapshot.one_percent:,.2f}"
        )
        if include_origin:
            lines.append(
                "🏁 Started $"
                + f"{REFERENCE_START_BALANCE:,.2f}"
                + f" · {REFERENCE_START_LABEL}"
            )
        return lines


__all__ = [
    "AccountLedgerSnapshot",
    "REFERENCE_START_BALANCE",
    "REFERENCE_START_LABEL",
    "TelegramTradeLedger",
    "TradeLedgerSnapshot",
    "money",
    "provider_badge",
]
