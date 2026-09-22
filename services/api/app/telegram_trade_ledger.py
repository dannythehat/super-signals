"""Telegram-only presentation ledger for the main Super Signals reference account.

This module does not alter broker execution, subscriber balances or user risk settings.
It exists only to present the owner's Super Signals track record consistently in the
member Telegram feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

SOFIA = ZoneInfo("Europe/Sofia")
REFERENCE_START_AT = datetime(2026, 8, 5, 21, 0, tzinfo=UTC)
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
            rows = session.execute(
                text(
                    """
                    WITH broker AS (
                        SELECT
                            timezone('Europe/Sofia',bd.occurred_at)::date AS day,
                            SUM(
                                COALESCE(bd.profit,0)
                                + COALESCE(bd.commission,0)
                                + COALESCE(bd.swap,0)
                            ) AS pnl
                        FROM broker_deals bd
                        JOIN sources src ON src.id=bd.source_id
                        WHERE bd.user_id=:user_id
                          AND src.status<>'revoked'
                          AND bd.entry_type='DEAL_ENTRY_OUT'
                          AND (bd.signal_id IS NOT NULL OR bd.broker_client_id LIKE 'SS_%')
                          AND bd.occurred_at>=:start_at
                          AND bd.occurred_at<:end_at
                          AND NOT EXISTS (
                              SELECT 1
                              FROM performance_reporting_overrides ro
                              WHERE ro.user_id=bd.user_id
                                AND (
                                  (
                                    ro.incident_key NOT LIKE 'restart-%'
                                    AND bd.occurred_at<ro.cutoff_at
                                    AND (
                                      bd.occurred_at AT TIME ZONE ro.timezone
                                    )::date=ro.reporting_date
                                  )
                                  OR
                                  (
                                    ro.incident_key LIKE 'restart-%'
                                    AND bd.occurred_at>=(
                                      ro.reporting_date::timestamp
                                      AT TIME ZONE ro.timezone
                                    )
                                    AND EXISTS (
                                      SELECT 1
                                      FROM positions rp
                                      WHERE rp.user_id=bd.user_id
                                        AND rp.id=bd.position_id
                                        AND COALESCE(rp.opened_at,rp.created_at)<ro.cutoff_at
                                    )
                                  )
                                )
                          )
                        GROUP BY 1
                    ),
                    reviewed AS (
                        SELECT
                            timezone('Europe/Sofia',pto.closed_at)::date AS day,
                            SUM(pto.cash_pnl) AS pnl
                        FROM performance_trade_outcomes pto
                        WHERE pto.user_id=:user_id
                          AND pto.closed_at>=:start_at
                          AND pto.closed_at<:end_at
                          AND pto.broker_deal_count=0
                          AND pto.close_reason LIKE 'reviewed_provider_%'
                        GROUP BY 1
                    ),
                    overrides AS (
                        SELECT ro.reporting_date AS day,SUM(ro.realised_cash_pnl) AS pnl
                        FROM performance_reporting_overrides ro
                        WHERE ro.user_id=:user_id
                          AND (
                            ro.reporting_date::timestamp AT TIME ZONE ro.timezone
                          )>=:start_at
                          AND (
                            ro.reporting_date::timestamp AT TIME ZONE ro.timezone
                          )<:end_at
                        GROUP BY 1
                    ),
                    daily AS (
                        SELECT day,SUM(pnl) AS pnl
                        FROM (
                            SELECT * FROM broker
                            UNION ALL
                            SELECT * FROM reviewed
                            UNION ALL
                            SELECT * FROM overrides
                        ) values_by_source
                        GROUP BY day
                    )
                    SELECT
                        COALESCE(SUM(pnl),0) AS total_pnl,
                        COALESCE(SUM(pnl) FILTER (WHERE day=:local_day),0) AS today_pnl,
                        COALESCE(
                            SUM(pnl) FILTER (WHERE day>=:month_start AND day<=:local_day),
                            0
                        ) AS month_to_date_pnl
                    FROM daily
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "start_at": REFERENCE_START_AT,
                    "end_at": point,
                    "local_day": local_day,
                    "month_start": month_start,
                },
            ).mappings().one()

        total_pnl = Decimal(str(rows["total_pnl"] or 0))
        balance = (REFERENCE_START_BALANCE + total_pnl).quantize(Decimal("0.01"))
        return AccountLedgerSnapshot(
            balance=balance,
            today_pnl=Decimal(str(rows["today_pnl"] or 0)).quantize(Decimal("0.01")),
            month_to_date_pnl=Decimal(
                str(rows["month_to_date_pnl"] or 0)
            ).quantize(Decimal("0.01")),
            one_percent=(balance * Decimal("0.01")).quantize(Decimal("0.01")),
            local_weekday=local_point.weekday(),
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
        lines.append(
            "💰 Super Signals balance: $"
            + f"{snapshot.balance:,.2f}"
            + " · 1% reference = $"
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
    "REFERENCE_START_AT",
    "REFERENCE_START_BALANCE",
    "REFERENCE_START_LABEL",
    "TelegramTradeLedger",
    "TradeLedgerSnapshot",
    "money",
    "provider_badge",
]
