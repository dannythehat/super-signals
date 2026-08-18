"""Fast read-only summary for the dashboard's live Today strip.

A trade is one signal that actually produced at least one broker position. TP tranches do
not inflate the trade count. Pending-only broker orders are reported separately until
filled. Completed signal outcomes are derived only from broker-backed position outcomes;
unknown reconciliation state is labelled settling rather than guessed as breakeven.
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
                            ) AS known_pnl
                        FROM trade_signals AS ts
                        JOIN positions AS p
                          ON p.signal_id = ts.signal_id
                         AND p.user_id = :user_id
                        LEFT JOIN performance_trade_outcomes AS o
                          ON o.position_id = p.id
                         AND o.user_id = p.user_id
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
                        COALESCE(SUM(known_pnl), 0) AS realised_pnl
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
        )


__all__ = [
    "TodayTradingSummary",
    "TodayTradingSummaryService",
    "local_day_bounds",
]
