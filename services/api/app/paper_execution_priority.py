"""Shared fresh market-entry policy used by paper and future LIVE execution.

Fresh market instructions execute at the broker's current executable quote; provider
entry text remains evidence/context and is not a second submission veto. Explicit
pending orders keep their literal broker-side prices. Final SL/TP geometry is validated
against the fresh executable quote before any market mutation.

Despite the legacy module/class name, this is current shared policy, not a paper-only
fork. The name will disappear only when the large canonical executor can be moved as one
verified source-preserving refactor; no compatibility alias is installed here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.mt5_execution_day26 import Day26ExecutionError, _SignalInput
from app.mt5_read_service_day23 import Day23LiveState, Day23Mt5ReadService, Day23ReadError
from app.paper_critical_execution import PaperCriticalExecutionService, _CriticalSignal

DEFAULT_PAPER_MAX_SIGNAL_AGE_SECONDS = 90.0


def _ordered_targets(side: str, targets: tuple[Decimal, ...]) -> bool:
    if not targets:
        return False
    if side == "BUY":
        return all(right > left for left, right in zip(targets, targets[1:]))
    if side == "SELL":
        return all(right < left for left, right in zip(targets, targets[1:]))
    return False


def _live_directionally_valid(
    *,
    side: str,
    entry: Decimal,
    stop_loss: Decimal,
    take_profits: tuple[Decimal, ...],
) -> bool:
    if side == "BUY":
        return (
            stop_loss < entry
            and all(tp > entry for tp in take_profits)
            and _ordered_targets(side, take_profits)
        )
    if side == "SELL":
        return (
            stop_loss > entry
            and all(tp < entry for tp in take_profits)
            and _ordered_targets(side, take_profits)
        )
    return False


class PaperExecutionPriorityService(PaperCriticalExecutionService):
    """Execute fresh market instructions at current broker truth."""

    def __init__(
        self,
        *,
        max_signal_age_seconds: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        raw = (
            max_signal_age_seconds
            if max_signal_age_seconds is not None
            else os.getenv("SUPER_SIGNALS_PAPER_MAX_SIGNAL_AGE_SECONDS", "90")
        )
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            parsed = DEFAULT_PAPER_MAX_SIGNAL_AGE_SECONDS
        if parsed <= 0:
            parsed = DEFAULT_PAPER_MAX_SIGNAL_AGE_SECONDS
        self._paper_max_signal_age_seconds = parsed

    def _load_critical_signal(self, signal_id: UUID) -> _CriticalSignal:
        critical = super()._load_critical_signal(signal_id)
        self._assert_signal_recent(critical.base)
        return critical

    def _assert_signal_still_current(
        self,
        owner_user_id: UUID,
        signal: _SignalInput,
    ) -> None:
        super()._assert_signal_still_current(owner_user_id, signal)
        self._assert_signal_recent(signal)

    def _assert_signal_recent(self, signal: _SignalInput) -> None:
        posted_at = signal.source_posted_at
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - posted_at.astimezone(UTC)).total_seconds()
        if age_seconds > self._paper_max_signal_age_seconds:
            raise Day26ExecutionError("paper_signal_stale_by_time")

    async def _resolve_entry(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        day23: Day23Mt5ReadService,
        initial_state: Day23LiveState,
    ) -> tuple[Decimal, Day23LiveState]:
        del owner_user_id, day23
        self._assert_signal_recent(signal)
        try:
            executable = Decimal(
                str(Day23Mt5ReadService.executable_price(initial_state, signal.side))
            )
        except Day23ReadError as exc:
            raise Day26ExecutionError(exc.code) from exc

        if not _live_directionally_valid(
            side=signal.side,
            entry=executable,
            stop_loss=signal.stop_loss,
            take_profits=signal.take_profits,
        ):
            raise Day26ExecutionError("strict_directional_validation_failed")
        return executable, initial_state

    def _validate_entry_timing(
        self,
        entries,
        side: str,
        current: Decimal,
    ) -> None:
        """Only literal pending orders are price-position constrained locally."""
        for entry in entries:
            if entry.order_type == "market":
                continue
            if entry.order_type == "buy_limit" and not entry.price < current:
                raise Day26ExecutionError("pending_entry_no_longer_valid")
            if entry.order_type == "buy_stop" and not entry.price > current:
                raise Day26ExecutionError("pending_entry_no_longer_valid")
            if entry.order_type == "sell_limit" and not entry.price > current:
                raise Day26ExecutionError("pending_entry_no_longer_valid")
            if entry.order_type == "sell_stop" and not entry.price < current:
                raise Day26ExecutionError("pending_entry_no_longer_valid")
            if side == "BUY" and not entry.order_type.startswith("buy_"):
                raise Day26ExecutionError("pending_side_mismatch")
            if side == "SELL" and not entry.order_type.startswith("sell_"):
                raise Day26ExecutionError("pending_side_mismatch")


__all__ = ["PaperExecutionPriorityService", "_live_directionally_valid"]
