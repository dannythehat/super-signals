"""Canonical mobile-dashboard view for paper and LIVE trading.

Open positions and executable broker state remain MetaAPI truth. The Owner demo balance
uses Super Signals accounting truth because manual broker resets would otherwise corrupt
both display and 1%-per-leg sizing. LIVE account balance remains the actual broker balance.
Performance accounting itself is shared across both modes and excludes capital movements
from profit/loss.

A transient MetaAPI read failure must never make an already broker-mapped Super Signals
position disappear from the app. During a live-read outage we expose the durable local
broker mapping with live price/P&L left unknown. Once broker reads recover, broker state
immediately resumes authority for positions/prices.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.dashboard_day32 import (
    Day32DashboardService,
    Day32DashboardView,
    Day32OpenPosition,
)
from app.dashboard_today_summary import TodayTradingSummaryService
from app.paper_run_epoch import active_paper_epoch
from app.trading_accounting import CanonicalTradingAccountingService


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class CanonicalDashboardRuntimeService(Day32DashboardService):
    """Day32 broker view with shared canonical trading accounting."""

    async def read(self, user_id: UUID) -> Day32DashboardView:
        view = await super().read(user_id)
        if view.account is None:
            return view
        accounting = CanonicalTradingAccountingService(self._session_factory)
        display_balance = float(
            accounting.displayed_balance(
                user_id,
                broker_balance=view.account.balance,
            )
        )
        visible_floating_pnl = sum(
            float(position.profit)
            for position in view.open_positions
            if position.profit is not None
        )
        display_equity = display_balance + visible_floating_pnl
        display_free_margin = display_equity - float(view.account.margin)
        return replace(
            view,
            account=replace(
                view.account,
                balance=display_balance,
                # Equity is canonical realised Balance plus precisely the
                # floating P/L of the active positions visible in this view.
                # Raw broker equity may contain credit or settling remnants and
                # must not contradict the position list shown to the user.
                equity=display_equity,
                free_margin=display_free_margin,
            ),
        )

    def _eligible_signal_ids(self, user_id: UUID) -> set[UUID] | None:
        """Visible signals include post-origin signals and genuine carry-over positions."""
        epoch = active_paper_epoch(user_id)
        if epoch is None:
            return None
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT DISTINCT s.id
                    FROM signals AS s
                    JOIN sources AS src ON src.id=s.source_id
                    WHERE src.status<>'revoked'
                      AND (
                            COALESCE(s.source_posted_at,s.created_at)>=:cutoff
                            OR EXISTS (
                                SELECT 1
                                FROM positions AS p
                                WHERE p.user_id=:user_id
                                  AND p.signal_id=s.id
                                  AND p.status IN ('open','pending')
                            )
                            OR EXISTS (
                                SELECT 1
                                FROM performance_trade_outcomes AS o
                                WHERE o.user_id=:user_id
                                  AND o.signal_id=s.id
                                  AND (
                                        (
                                            o.status IN ('won','lost','breakeven')
                                            AND o.closed_at>=:cutoff
                                        )
                                        OR o.status IN ('open','pending')
                                        OR (
                                            o.status='closed_unknown'
                                            AND COALESCE(o.closed_at,o.derived_at)>=:cutoff
                                        )
                                  )
                            )
                      )
                    """
                ),
                {"cutoff": epoch.started_at, "user_id": user_id},
            ).scalars().all()
        return {UUID(str(value)) for value in values}

    def _mapped_open_positions(self, user_id: UUID, broker_positions):  # noqa: ANN001
        values = super()._mapped_open_positions(user_id, broker_positions)
        eligible = self._eligible_signal_ids(user_id)
        if eligible is None:
            return values
        return tuple(item for item in values if item.signal_id in eligible)

    def _durable_mapped_open_positions(self, user_id: UUID) -> tuple[Day32OpenPosition, ...]:
        """Last broker-mapped local truth used only while the broker read is unavailable."""
        eligible = self._eligible_signal_ids(user_id)
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        p.id,p.signal_id,p.tp_index,p.planned_risk_percent,
                        p.broker_position_id,p.volume,p.entry_price,p.stop_loss,
                        p.take_profit,p.opened_at,s.symbol,s.side
                    FROM positions AS p
                    JOIN signals AS s ON s.id=p.signal_id
                    WHERE p.user_id=:user_id
                      AND p.status='open'
                      AND p.broker_position_id IS NOT NULL
                      AND p.volume IS NOT NULL
                      AND p.entry_price IS NOT NULL
                    ORDER BY p.opened_at NULLS LAST,p.entry_index,p.tp_index,p.id
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
        values: list[Day32OpenPosition] = []
        for row in rows:
            signal_id = UUID(str(row["signal_id"]))
            if eligible is not None and signal_id not in eligible:
                continue
            values.append(
                Day32OpenPosition(
                    position_id=UUID(str(row["id"])),
                    broker_position_id=str(row["broker_position_id"]),
                    signal_id=signal_id,
                    tp_index=int(row["tp_index"]),
                    symbol=str(row["symbol"] or ""),
                    side=str(row["side"] or ""),
                    volume=float(row["volume"]),
                    planned_risk_percent=Decimal(str(row["planned_risk_percent"])),
                    entry_price=float(row["entry_price"]),
                    current_price=None,
                    stop_loss=(float(row["stop_loss"]) if row["stop_loss"] is not None else None),
                    take_profit=(float(row["take_profit"]) if row["take_profit"] is not None else None),
                    profit=None,
                    opened_at=row["opened_at"],
                )
            )
        return tuple(values)

    def _without_live_state(self, *, connection, trading, user_id: UUID, now: datetime):  # noqa: ANN001,ANN201
        view: Day32DashboardView = super()._without_live_state(
            connection=connection,
            trading=trading,
            user_id=user_id,
            now=now,
        )
        if not connection.configured:
            return view
        durable = self._durable_mapped_open_positions(user_id)
        if not durable:
            return view
        return replace(
            view,
            open_positions=durable,
            open_profit=None,
        )

    def _latest_signal(self, user_id: UUID):  # noqa: ANN201
        value = super()._latest_signal(user_id)
        eligible = self._eligible_signal_ids(user_id)
        if eligible is None or value is None:
            return value
        return value if value.signal_id in eligible else None

    def _recent_completed(self, user_id: UUID):  # noqa: ANN201
        values = super()._recent_completed(user_id)
        eligible = self._eligible_signal_ids(user_id)
        if eligible is None:
            return values
        return tuple(item for item in values if item.signal_id in eligible)

    def _activity(self, user_id: UUID):  # noqa: ANN201
        values = super()._activity(user_id)
        epoch = active_paper_epoch(user_id)
        if epoch is None:
            return values
        return tuple(item for item in values if _utc(item.created_at) >= epoch.started_at)


class CanonicalTodayTradingSummaryService(TodayTradingSummaryService):
    """Today summary whose Owner session begins at the accepted opening balance.

    Signals already open at that boundary remain legitimate trades. If they close after
    the boundary their post-boundary outcome is added to Today's trade/win/loss/pip counts,
    matching the cash ledger used by both the app and website.
    """

    def _session_start(
        self,
        session,
        user_id: UUID,
        *,
        day_start: datetime,
        day_end: datetime,
    ) -> datetime:
        start = super()._session_start(
            session,
            user_id,
            day_start=day_start,
            day_end=day_end,
        )
        epoch = active_paper_epoch(user_id)
        if epoch is None:
            return start
        if _utc(day_start) <= epoch.started_at < _utc(day_end):
            return max(_utc(start), epoch.started_at)
        return start

    def read(
        self,
        user_id: UUID,
        *,
        timezone_name: str = "UTC",
        now_utc: datetime | None = None,
    ):
        base = super().read(
            user_id,
            timezone_name=timezone_name,
            now_utc=now_utc,
        )
        epoch = active_paper_epoch(user_id)
        if epoch is None:
            return base

        start = max(_utc(base.session_started_at), epoch.started_at)
        point = _utc(now_utc or datetime.now(UTC))
        if point <= start:
            return base

        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    WITH carry_signals AS (
                        SELECT DISTINCT s.id AS signal_id
                        FROM signals AS s
                        JOIN sources AS src ON src.id=s.source_id
                        JOIN positions AS p ON p.signal_id=s.id AND p.user_id=:user_id
                        WHERE src.status<>'revoked'
                          AND COALESCE(s.source_posted_at,s.created_at)<:start_at
                    ),
                    outcome_values AS (
                        SELECT
                            o.signal_id,
                            o.status,
                            o.closed_at,
                            COALESCE(o.cash_pnl,0) AS cash_pnl,
                            COALESCE(
                                o.net_pips,
                                CASE
                                    WHEN UPPER(o.symbol)='XAUUSD'
                                     AND o.entry_price IS NOT NULL
                                     AND o.exit_price IS NOT NULL
                                    THEN CASE
                                        WHEN UPPER(o.side)='BUY' THEN (o.exit_price-o.entry_price)/0.1
                                        WHEN UPPER(o.side)='SELL' THEN (o.entry_price-o.exit_price)/0.1
                                        ELSE NULL
                                    END
                                    ELSE NULL
                                END
                            ) AS effective_pips
                        FROM performance_trade_outcomes AS o
                        JOIN carry_signals AS c ON c.signal_id=o.signal_id
                        WHERE o.user_id=:user_id
                    ),
                    carry AS (
                        SELECT
                            c.signal_id,
                            COUNT(*) FILTER (
                                WHERE o.status IN ('won','lost','breakeven')
                                  AND o.closed_at>=:start_at
                                  AND o.closed_at<:end_at
                            )::int AS post_outcomes,
                            COUNT(*) FILTER (WHERE o.status='open')::int AS open_legs,
                            COUNT(*) FILTER (WHERE o.status='pending')::int AS pending_legs,
                            COUNT(*) FILTER (
                                WHERE o.status='closed_unknown'
                                  AND COALESCE(o.closed_at,:end_at)>=:start_at
                            )::int AS settling_legs,
                            COALESCE(SUM(o.cash_pnl) FILTER (
                                WHERE o.status IN ('won','lost','breakeven')
                                  AND o.closed_at>=:start_at
                                  AND o.closed_at<:end_at
                            ),0) AS post_pnl,
                            COALESCE(SUM(o.effective_pips) FILTER (
                                WHERE o.status IN ('won','lost','breakeven')
                                  AND o.closed_at>=:start_at
                                  AND o.closed_at<:end_at
                            ),0) AS post_pips,
                            COALESCE(SUM(o.effective_pips) FILTER (
                                WHERE o.status='won'
                                  AND o.closed_at>=:start_at
                                  AND o.closed_at<:end_at
                                  AND o.effective_pips>0
                            ),0) AS winning_pips
                        FROM carry_signals AS c
                        LEFT JOIN outcome_values AS o ON o.signal_id=c.signal_id
                        GROUP BY c.signal_id
                    )
                    SELECT * FROM carry
                    WHERE post_outcomes>0 OR open_legs>0 OR pending_legs>0 OR settling_legs>0
                    """
                ),
                {"user_id": user_id, "start_at": start, "end_at": point},
            ).mappings().all()

        if not rows:
            return base

        extra_trades = len(rows)
        extra_open = sum(1 for row in rows if int(row["open_legs"] or 0) > 0)
        extra_settling = sum(
            1
            for row in rows
            if int(row["open_legs"] or 0) == 0
            and int(row["pending_legs"] or 0) == 0
            and int(row["settling_legs"] or 0) > 0
        )
        closed_rows = [
            row
            for row in rows
            if int(row["post_outcomes"] or 0) > 0
            and int(row["open_legs"] or 0) == 0
            and int(row["pending_legs"] or 0) == 0
            and int(row["settling_legs"] or 0) == 0
        ]
        extra_wins = sum(1 for row in closed_rows if Decimal(str(row["post_pnl"] or 0)) > 0)
        extra_losses = sum(1 for row in closed_rows if Decimal(str(row["post_pnl"] or 0)) < 0)
        extra_breakeven = sum(1 for row in closed_rows if Decimal(str(row["post_pnl"] or 0)) == 0)
        extra_pnl = sum((Decimal(str(row["post_pnl"] or 0)) for row in rows), Decimal("0"))
        extra_pips = sum((Decimal(str(row["post_pips"] or 0)) for row in rows), Decimal("0"))
        extra_winning_pips = sum(
            (Decimal(str(row["winning_pips"] or 0)) for row in rows),
            Decimal("0"),
        )

        return replace(
            base,
            trades=base.trades + extra_trades,
            wins=base.wins + extra_wins,
            losses=base.losses + extra_losses,
            breakeven=base.breakeven + extra_breakeven,
            open=base.open + extra_open,
            settling=base.settling + extra_settling,
            realised_pnl=base.realised_pnl + extra_pnl,
            winning_pips=base.winning_pips + extra_winning_pips,
            net_pips=base.net_pips + extra_pips,
        )


__all__ = [
    "CanonicalDashboardRuntimeService",
    "CanonicalTodayTradingSummaryService",
]
