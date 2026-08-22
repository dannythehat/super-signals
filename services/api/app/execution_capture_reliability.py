"""Shared capture-first retry policy for canonical paper and LIVE execution.

A transient MetaAPI outage must not permanently lose a fresh provider trade. Retrying is
allowed only when the previous attempt is provably broker-free: there is no later provider
close/cancel and every local execution row is either absent or an unlinked error row.
Broker-linked, compensated, pending, open, closed or otherwise ambiguous state is never
retried here.

Paper and LIVE share the same execution engine. The Owner demo substitutes its canonical
Super Signals balance because manual demo resets are not trading P/L. LIVE accounts keep
their actual broker balance. In both modes the number displayed as balance is therefore
the same number supplied to the 1%-per-leg risk sizer.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.mt5_execution_day26 import Day26ExecutionError, _SignalInput
from app.risk_sizing_day24 import Day24RiskSizingResult
from app.trading_accounting import CanonicalTradingAccountingService
from app.trading_execution_canonical import (
    CanonicalTradingExecutionService,
    MemberTradingExecutionService,
)

_TRANSIENT_READ_OR_ROUTE_ERRORS = {
    "metaapi_timeout",
    "metaapi_unreachable",
    "metaapi_temporarily_unavailable",
}
_MAX_CAPTURE_ATTEMPTS = 4
_RETRY_DELAY_SECONDS = 0.5
_SIZING_USER_ID: ContextVar[UUID | None] = ContextVar(
    "super_signals_sizing_user_id",
    default=None,
)


class _CaptureRetryMixin:
    async def execute_owner_demo_signal(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        risk_percent,
        double_lot_approved: bool,
    ):
        context_token = _SIZING_USER_ID.set(owner_user_id)
        try:
            for attempt in range(1, _MAX_CAPTURE_ATTEMPTS + 1):
                try:
                    return await super().execute_owner_demo_signal(
                        owner_user_id=owner_user_id,
                        signal_id=signal_id,
                        risk_percent=risk_percent,
                        double_lot_approved=double_lot_approved,
                    )
                except Day26ExecutionError as exc:
                    if (
                        exc.code not in _TRANSIENT_READ_OR_ROUTE_ERRORS
                        or attempt >= _MAX_CAPTURE_ATTEMPTS
                        or not self._prepare_clean_retry(owner_user_id, signal_id)
                    ):
                        raise
                    self._audit(
                        owner_user_id=owner_user_id,
                        signal_id=signal_id,
                        event_type="mt5.canonical_transient_execution_retry",
                        payload={
                            "failed_attempt": attempt,
                            "next_attempt": attempt + 1,
                            "error_code": exc.code,
                            "broker_mutation_present": False,
                            "automatic_retry": True,
                            "capture_first": True,
                        },
                    )
                    await asyncio.sleep(_RETRY_DELAY_SECONDS * attempt)

            raise Day26ExecutionError("canonical_execution_retry_exhausted")
        finally:
            _SIZING_USER_ID.reset(context_token)

    def _size_signal(
        self,
        *,
        signal: _SignalInput,
        execution_entry: Decimal,
        balance: float,
        price_loss_tick_value: float | None,
        specification: dict[str, object],
        risk_percent: Decimal | str | float,
        double_lot_approved: bool,
    ) -> Day24RiskSizingResult:
        user_id = _SIZING_USER_ID.get()
        if user_id is not None:
            accounting = CanonicalTradingAccountingService(self._session_factory)
            balance = float(
                accounting.displayed_balance(
                    user_id,
                    broker_balance=balance,
                )
            )
        return super()._size_signal(
            signal=signal,
            execution_entry=execution_entry,
            balance=balance,
            price_loss_tick_value=price_loss_tick_value,
            specification=specification,
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )

    def _prepare_clean_retry(self, user_id: UUID, signal_id: UUID) -> bool:
        """Delete only broker-free error debris after proving the signal remains active."""
        with self._session_factory() as session:
            provider_ended = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM signal_lifecycle_events
                            WHERE signal_id=:signal_id
                              AND event_type IN ('cancel','close_instruction')
                        )
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalar_one()
            )
            if provider_ended:
                return False

            rows = session.execute(
                text(
                    """
                    SELECT id,status,broker_order_id,broker_position_id
                    FROM positions
                    WHERE signal_id=:signal_id AND user_id=:user_id
                    """
                ),
                {"signal_id": signal_id, "user_id": user_id},
            ).mappings().all()
            if not rows:
                return True

            disposable = all(
                str(row["status"] or "") == "error"
                and not str(row["broker_order_id"] or "").strip()
                and not str(row["broker_position_id"] or "").strip()
                for row in rows
            )
            if not disposable:
                return False

            session.execute(
                text(
                    """
                    DELETE FROM positions
                    WHERE signal_id=:signal_id
                      AND user_id=:user_id
                      AND status='error'
                      AND broker_order_id IS NULL
                      AND broker_position_id IS NULL
                    """
                ),
                {"signal_id": signal_id, "user_id": user_id},
            )
            session.commit()
        return True


class CaptureReliableCanonicalTradingExecutionService(
    _CaptureRetryMixin,
    CanonicalTradingExecutionService,
):
    """Owner demo canonical engine with safe transient capture retry."""


class CaptureReliableMemberTradingExecutionService(
    _CaptureRetryMixin,
    MemberTradingExecutionService,
):
    """Eligible member LIVE canonical engine with the identical retry contract."""


__all__ = [
    "CaptureReliableCanonicalTradingExecutionService",
    "CaptureReliableMemberTradingExecutionService",
]
