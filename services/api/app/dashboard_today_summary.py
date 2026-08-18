"""Fast broker-backed summary for the dashboard's live Today strip.

One provider signal is one trade even when it has several TP legs. Cash P/L and pips are
derived from the same broker-backed outcomes. Account balance movements are reconciled
separately so a credit/reset/correction can never masquerade as trading profit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True, slots=True)
class TodayTradingSummary:
    timezone: str
    trades: int
    wins: int
    losses: int
    breakeven: int
    open: int
    pending: int
    settling: int
    realised_pnl: Decimal
    winning_pips: Decimal
    net_pips: Decimal
    balance_change: Decimal | None
    account_adjustment: Decimal | None
    reconciliation_gap: Decimal | None
    reconciliation_ready: bool
    reconciled: bool


def local_day_bounds(
    timezone_name: str,
    *,
    now_utc: datetime | None = None,
) -> tuple[str, datetime, datetime]:
    """Return the requested local calendar day as an exact UTC half-open interval."""
    requested = (timezone_name or "UTC").strip() or "UTC"
    try:
        zone = ZoneInfo(requested)
        resolved = requested
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
        resolved = "UTC"

    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_now = now.astimezone(zone)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return resolved, start_local.astimezone(UTC), end_local.astimezone(UTC)


class TodayTradingSummaryService:
    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def read(
        self,
        user_id: UUID,
        *,
        timezone_name: str = "UTC",
        now_utc: datetime | None = None,
    ) -> TodayTradingSummary:
        resolved_timezone, start_utc, end_utc = local_day_bounds(
            timezone_name,
            now_utc=now_utc,
        )
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    WITH trade_signals AS (
                        SELECT
                            p.signal_id,
                            MIN(COALESCE(p.opened_at, p.created_at)) AS started_at
                        FROM positions AS p
                        WHERE p.user_id = :user_id
                          AND p.broker_position_id IS NOT NULL
                        GROUP BY p.signal_id
                        HAVING MIN(COALESCE(p.opened_at, p.created_at)) >= :start_utc
                           AND MIN(COALESCE(p.opened_at, p.created_at)) < :end_utc
                    ),
                    outcome_values AS (
                        SELECT
                            o.*,
                            COALESCE(
                                o.net_pips,
                                CASE
                                    WHEN UPPER(o.symbol) = 'XAUUSD'
                                     AND o.entry_price IS NOT NULL
                                     AND o.exit_price IS NOT NULL
                                    THEN CASE
                                        WHEN UPPER(o.side) = 'BUY'
                                            THEN (o.exit_price - o.entry_price) / 0.1
                                        WHEN UPPER(o.side) = 'SELL'
                                            THEN (o.entry_price - o.exit_price) / 0.1
                                        ELSE NULL
                                    END
                                    ELSE NULL
                                END
                            ) AS effective_pips
                        FROM performance_trade_outcomes AS o
                        WHERE o.user_id = :user_id
                    ),
                    per_trade AS (
                        SELECT
                            ts.signal_id,
                            COUNT(*) FILTER (
                                WHERE p.broker_position_id IS NOT NULL
                            ) AS broker_legs,
                            COUNT(*) FILTER (
                                WHERE p.status = 'open'
                                  AND p.broker_position_id IS NOT NULL
                            ) AS open_legs,
                            COUNT(o.position_id) FILTER (
                                WHERE p.broker_position_id IS NOT NULL
                                  AND o.status IN ('won', 'lost', 'breakeven', 'closed_unknown')
                            ) AS terminal_outcomes,
                            COUNT(o.position_id) FILTER (
                                WHERE p.broker_position_id IS NOT NULL
                                  AND o.status = 'closed_unknown'
                            ) AS unknown_outcomes,
                            COALESCE(
                                SUM(o.cash_pnl) FILTER (
                                    WHERE p.broker_position_id IS NOT NULL
                                      AND o.status IN ('won', 'lost', 'breakeven')
                                ),
                                0
                            ) AS known_pnl,
                            COALESCE(
                                SUM(o.effective_pips) FILTER (
                                    WHERE p.broker_position_id IS NOT NULL
                                      AND o.status IN ('won', 'lost', 'breakeven')
                                ),
                                0
                            ) AS known_pips,
                            COALESCE(
                                SUM(o.effective_pips) FILTER (
                                    WHERE p.broker_position_id IS NOT NULL
                                      AND o.status = 'won'
                                      AND o.effective_pips > 0
                                ),
                                0
                            ) AS winning_pips
                        FROM trade_signals AS ts
                        JOIN positions AS p
                          ON p.signal_id = ts.signal_id
                         AND p.user_id = :user_id
                        LEFT JOIN outcome_values AS o
                          ON o.position_id = p.id
                        GROUP BY ts.signal_id
                    )
                    SELECT
                        COUNT(*)::int AS trades,
                        COUNT(*) FILTER (WHERE open_legs > 0)::int AS open,
                        COUNT(*) FILTER (
                            WHERE open_legs = 0
                              AND terminal_outcomes = broker_legs
                              AND unknown_outcomes = 0
                              AND known_pnl > 0
                        )::int AS wins,
                        COUNT(*) FILTER (
                            WHERE open_legs = 0
                              AND terminal_outcomes = broker_legs
                              AND unknown_outcomes = 0
                              AND known_pnl < 0
                        )::int AS losses,
                        COUNT(*) FILTER (
                            WHERE open_legs = 0
                              AND terminal_outcomes = broker_legs
                              AND unknown_outcomes = 0
                              AND known_pnl = 0
                        )::int AS breakeven,
                        COUNT(*) FILTER (
                            WHERE open_legs = 0
                              AND (
                                  terminal_outcomes < broker_legs
                                  OR unknown_outcomes > 0
                              )
                        )::int AS settling,
                        COALESCE(SUM(known_pnl), 0) AS realised_pnl,
                        COALESCE(SUM(winning_pips), 0) AS winning_pips,
                        COALESCE(SUM(known_pips), 0) AS net_pips
                    FROM per_trade
                    """
                ),
                {
                    "user_id": user_id,
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                },
            ).mappings().one()
            pending = int(
                session.execute(
                    text(
                        """
                        SELECT COUNT(DISTINCT p.signal_id)::int
                        FROM positions AS p
                        WHERE p.user_id = :user_id
                          AND p.status = 'pending'
                          AND p.broker_order_id IS NOT NULL
                          AND p.created_at >= :start_utc
                          AND p.created_at < :end_utc
                        """
                    ),
                    {
                        "user_id": user_id,
                        "start_utc": start_utc,
                        "end_utc": end_utc,
                    },
                ).scalar_one()
                or 0
            )
            reconciliation_ready = bool(
                session.execute(
                    text(
                        """
                        SELECT 1
                        FROM audit_events
                        WHERE actor_user_id=:user_id
                          AND event_type='mt5.performance_full_history_backfilled'
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one_or_none()
            )
            account_row = session.execute(
                text(
                    """
                    WITH first_snapshot AS (
                        SELECT balance,captured_at
                        FROM performance_account_snapshots
                        WHERE user_id=:user_id
                          AND captured_at>=:start_utc
                          AND captured_at<:end_utc
                        ORDER BY captured_at ASC
                        LIMIT 1
                    ),
                    last_snapshot AS (
                        SELECT balance,captured_at
                        FROM performance_account_snapshots
                        WHERE user_id=:user_id
                          AND captured_at>=:start_utc
                          AND captured_at<:end_utc
                        ORDER BY captured_at DESC
                        LIMIT 1
                    )
                    SELECT
                        f.balance AS opening_balance,
                        l.balance AS closing_balance,
                        f.captured_at AS first_at,
                        l.captured_at AS last_at,
                        COALESCE((
                            SELECT SUM(d.profit+d.commission+d.swap)
                            FROM broker_deals d
                            WHERE d.user_id=:user_id
                              AND d.occurred_at>f.captured_at
                              AND d.occurred_at<=l.captured_at
                              AND d.broker_position_id IS NULL
                              AND d.symbol IS NULL
                              AND UPPER(COALESCE(d.entry_type,'')) NOT LIKE 'DEAL_ENTRY_%'
                        ),0) AS account_adjustment,
                        COALESCE((
                            SELECT SUM(d.profit+d.commission+d.swap)
                            FROM broker_deals d
                            WHERE d.user_id=:user_id
                              AND d.occurred_at>f.captured_at
                              AND d.occurred_at<=l.captured_at
                        ),0) AS all_deal_cash
                    FROM first_snapshot f CROSS JOIN last_snapshot l
                    """
                ),
                {"user_id": user_id, "start_utc": start_utc, "end_utc": end_utc},
            ).mappings().first()

        balance_change: Decimal | None = None
        account_adjustment: Decimal | None = None
        reconciliation_gap: Decimal | None = None
        reconciled = False
        if (
            account_row is not None
            and account_row["first_at"] != account_row["last_at"]
            and reconciliation_ready
        ):
            balance_change = Decimal(str(account_row["closing_balance"])) - Decimal(
                str(account_row["opening_balance"])
            )
            account_adjustment = Decimal(str(account_row["account_adjustment"] or 0))
            reconciliation_gap = balance_change - Decimal(
                str(account_row["all_deal_cash"] or 0)
            )
            reconciled = abs(reconciliation_gap) <= Decimal("0.01")

        return TodayTradingSummary(
            timezone=resolved_timezone,
            trades=int(row["trades"] or 0),
            wins=int(row["wins"] or 0),
            losses=int(row["losses"] or 0),
            breakeven=int(row["breakeven"] or 0),
            open=int(row["open"] or 0),
            pending=pending,
            settling=int(row["settling"] or 0),
            realised_pnl=Decimal(str(row["realised_pnl"] or 0)),
            winning_pips=Decimal(str(row["winning_pips"] or 0)),
            net_pips=Decimal(str(row["net_pips"] or 0)),
            balance_change=balance_change,
            account_adjustment=account_adjustment,
            reconciliation_gap=reconciliation_gap,
            reconciliation_ready=reconciliation_ready,
            reconciled=reconciled,
        )


__all__ = [
    "TodayTradingSummary",
    "TodayTradingSummaryService",
    "local_day_bounds",
]
