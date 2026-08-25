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
        broker_balance = float(view.account.balance)
        delta = display_balance - broker_balance
        display_equity = (
            float(view.account.equity) + delta
            if view.open_positions
            else display_balance
        )
        return replace(
            view,
            account=replace(
                view.account,
                balance=display_balance,
                # Only a currently active, app-mapped broker position may create
                # floating P/L. Settling/closed/unmapped broker remnants must
                # never leave Equity different from Balance when Open is zero.
                equity=display_equity,
                free_margin=float(view.account.free_margin) + delta,
            ),
        )

    def _eligible_signal_ids(self, user_id: UUID) -> set[UUID] | None:
        epoch = active_paper_epoch(user_id)
        if epoch is None:
            return None
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT s.id
                    FROM signals AS s
                    JOIN sources AS src ON src.id=s.source_id
                    WHERE COALESCE(s.source_posted_at,s.created_at)>=:cutoff
                      AND src.status<>'revoked'
                    """
                ),
                {"cutoff": epoch.started_at},
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
    """Today summary whose Owner session cannot begin before the active paper epoch."""

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


__all__ = [
    "CanonicalDashboardRuntimeService",
    "CanonicalTodayTradingSummaryService",
]
