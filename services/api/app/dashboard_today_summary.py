"""Fast broker-backed summary for the dashboard's live Today strip.

One provider signal is one trade even when it has several TP legs. Cash P/L and pips are
derived from the same broker-backed outcomes. Revoked providers are outside the
user-facing performance universe and therefore contribute no trades, P/L or pips.

When a demo account is materially reset inside the local calendar day, the Today strip
starts at that reset so pre-reset trades cannot be compared with the new account balance.
Smaller balance movements which are not present in the complete broker deal history are
exposed separately as broker balance adjustments and never counted as trading profit.
Account-level reconciliation is deliberately withheld for a period containing a revoked
provider deal because literal broker balance movement includes that historical cash while
the performance view intentionally excludes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.reporting_overrides import current_day_override


@dataclass(frozen=True, slots=True)
class TodayTradingSummary:
    timezone: str
    session_started_at: datetime
    trades: int
    wins: int
    losses: int
    breakeven: int
    open: int
    settling: int
    realised_pnl: Decimal
    winning_pips: Decimal
    net_pips: Decimal
    balance_change: Decimal | None
    balance_adjustment: Decimal | None
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

    def _session_start(
        self,
        session: Session,
        user_id: UUID,
        *,
        day_start: datetime,
        day_end: datetime,
    ) -> datetime:
        """Use a material same-day demo balance reset as the performance boundary."""
        reset_at = session.execute(
            text(
                """
                WITH snapshots AS (
                    SELECT
                        captured_at,
                        balance,
                        LAG(balance) OVER (ORDER BY captured_at) AS previous_balance
                    FROM performance_account_snapshots
                    WHERE user_id=:user_id
                      AND captured_at>=:lookback_start
                      AND captured_at<:day_end
                )
                SELECT captured_at
                FROM snapshots
                WHERE captured_at>=:day_start
                  AND previous_balance IS NOT NULL
                  AND ABS(balance-previous_balance)>=100
                  AND ABS(balance-previous_balance)>=ABS(previous_balance)*0.20
                ORDER BY captured_at DESC
                LIMIT 1
                """
            ),
            {
                "user_id": user_id,
                "lookback_start": day_start - timedelta(hours=6),
                "day_start": day_start,
                "day_end": day_end,
            },
        ).scalar_one_or_none()
        return reset_at or day_start

    def read(
        self,
        user_id: UUID,
        *,
        timezone_name: str = "UTC",
        now_utc: datetime | None = None,
    ) -> TodayTradingSummary:
        resolved_timezone, day_start_utc, end_utc = local_day_bounds(
            timezone_name,
            now_utc=now_utc,
        )
        with self._session_factory() as session:
            reporting_override = current_day_override(
                session,
                user_id,
                day_start=day_start_utc,
            )
            start_utc = self._session_start(
                session,
                user_id,
                day_start=day_start_utc,
                day_end=end_utc,
            )
            row = session.execute(
                text(
                    """
                    WITH trade_signals AS (
                        SELECT
                            p.signal_id,
                            MIN(COALESCE(p.opened_at, p.created_at)) AS started_at
                        FROM positions AS p
                        JOIN signals AS s ON s.id=p.signal_id
                        JOIN sources AS src ON src.id=s.source_id
                        WHERE p.user_id = :user_id
                          AND p.broker_position_id IS NOT NULL
                          AND src.status <> 'revoked'
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
                        JOIN sources AS src_o ON src_o.id=o.source_id
                        WHERE o.user_id = :user_id
                          AND src_o.status <> 'revoked'
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
            post_cutover_cash = Decimal("0")
            if reporting_override is not None:
                post_cutover_cash = Decimal(
                    str(
                        session.execute(
                            text(
                                """
                                SELECT COALESCE(SUM(o.cash_pnl), 0)
                                FROM performance_trade_outcomes AS o
                                JOIN sources AS src ON src.id=o.source_id
                                WHERE o.user_id=:user_id
                                  AND src.status<>'revoked'
                                  AND o.status IN ('won','lost','breakeven')
                                  AND o.closed_at>=:cutoff_at
                                  AND o.closed_at<:end_utc
                                """
                            ),
                            {
                                "user_id": user_id,
                                "cutoff_at": reporting_override[2],
                                "end_utc": end_utc,
                            },
                        ).scalar_one()
                        or 0
                    )
                )
            history_backfilled = bool(
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
                              AND d.occurred_at>=f.captured_at
                              AND d.occurred_at<=l.captured_at
                        ),0) AS all_deal_cash,
                        COALESCE((
                            SELECT COUNT(*)
                            FROM broker_deals d
                            JOIN sources src ON src.id=d.source_id
                            WHERE d.user_id=:user_id
                              AND d.occurred_at>=f.captured_at
                              AND d.occurred_at<=l.captured_at
                              AND src.status='revoked'
                        ),0) AS revoked_deal_count
                    FROM first_snapshot f CROSS JOIN last_snapshot l
                    """
                ),
                {"user_id": user_id, "start_utc": start_utc, "end_utc": end_utc},
            ).mappings().first()

        revoked_deals_present = bool(
            account_row is not None and int(account_row["revoked_deal_count"] or 0) > 0
        )
        reconciliation_ready = history_backfilled and not revoked_deals_present
        balance_change: Decimal | None = None
        balance_adjustment: Decimal | None = None
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
            broker_deal_cash = Decimal(str(account_row["all_deal_cash"] or 0))
            balance_adjustment = balance_change - broker_deal_cash
            reconciliation_gap = Decimal("0")
            reconciled = True

        return TodayTradingSummary(
            timezone=resolved_timezone,
            session_started_at=start_utc,
            trades=int(row["trades"] or 0),
            wins=int(row["wins"] or 0),
            losses=int(row["losses"] or 0),
            breakeven=int(row["breakeven"] or 0),
            open=int(row["open"] or 0),
            settling=int(row["settling"] or 0),
            realised_pnl=(
                reporting_override[0] + post_cutover_cash
                if reporting_override is not None
                else Decimal(str(row["realised_pnl"] or 0))
            ),
            winning_pips=Decimal(str(row["winning_pips"] or 0)),
            net_pips=Decimal(str(row["net_pips"] or 0)),
            balance_change=balance_change,
            balance_adjustment=balance_adjustment,
            reconciliation_gap=reconciliation_gap,
            reconciliation_ready=reconciliation_ready,
            reconciled=reconciled,
        )


__all__ = [
    "TodayTradingSummary",
    "TodayTradingSummaryService",
    "local_day_bounds",
]
