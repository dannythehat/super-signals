"""Canonical mobile-dashboard view for the active paper-testing run.

The broker account remains the reconciliation authority, but the Owner paper UI is a
virtual test run beginning at the configured paper epoch. Pre-epoch broker/audit truth
is retained and simply excluded from the active dashboard. No runtime monkey patching is
used.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.dashboard_day32 import Day32DashboardService
from app.dashboard_today_summary import TodayTradingSummaryService
from app.paper_run_epoch import active_paper_epoch

_ZERO = Decimal("0")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class CanonicalDashboardRuntimeService(Day32DashboardService):
    """Day32 broker view with an explicit Owner paper-run visibility boundary."""

    def _eligible_signal_ids(self, user_id: UUID) -> set[UUID] | None:
        epoch = active_paper_epoch(user_id)
        if epoch is None:
            return None
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT id
                    FROM signals
                    WHERE COALESCE(source_posted_at,created_at)>=:cutoff
                    """
                ),
                {"cutoff": epoch.started_at},
            ).scalars().all()
        return {UUID(str(value)) for value in values}

    def _post_epoch_realised_cash(self, user_id: UUID) -> Decimal:
        epoch = active_paper_epoch(user_id)
        if epoch is None:
            return _ZERO
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(o.cash_pnl),0)
                    FROM performance_trade_outcomes AS o
                    JOIN signals AS s ON s.id=o.signal_id
                    WHERE o.user_id=:user_id
                      AND COALESCE(s.source_posted_at,s.created_at)>=:cutoff
                      AND o.status IN ('won','lost','breakeven')
                      AND o.cash_pnl IS NOT NULL
                    """
                ),
                {"user_id": user_id, "cutoff": epoch.started_at},
            ).scalar_one()
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))

    def _mapped_open_positions(self, user_id: UUID, broker_positions):  # noqa: ANN001
        values = super()._mapped_open_positions(user_id, broker_positions)
        eligible = self._eligible_signal_ids(user_id)
        if eligible is None:
            return values
        return tuple(item for item in values if item.signal_id in eligible)

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

    async def read(self, user_id: UUID):  # noqa: ANN201
        view = await super().read(user_id)
        epoch = active_paper_epoch(user_id)
        if epoch is None or view.account is None:
            return view

        realised = self._post_epoch_realised_cash(user_id)
        open_profit = sum(
            (
                Decimal(str(item.profit))
                for item in view.open_positions
                if item.profit is not None
            ),
            _ZERO,
        ).quantize(Decimal("0.01"))
        balance = (epoch.baseline_balance + realised).quantize(Decimal("0.01"))
        equity = (balance + open_profit).quantize(Decimal("0.01"))
        return replace(
            view,
            account=replace(
                view.account,
                balance=float(balance),
                equity=float(equity),
                margin=0.0,
                free_margin=float(equity),
            ),
            open_profit=float(open_profit),
        )


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
