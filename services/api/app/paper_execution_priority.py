"""Owner DEMO execution-priority policy for paper testing.

Paper testing needs to exercise provider trades, not discard fresh market signals because
price moved a few ticks while the automation was processing them or because the broker's
minimum 0.01 lot makes the realised risk exceed the configured percentage.

For the Owner DEMO path, configured risk is the TOTAL canonical-signal risk budget. It
is split across TP/runner legs rather than multiplied by them. This is deliberately
limited to Owner DEMO and does not alter future LIVE-member execution.

Pending orders keep their literal provider prices and broker-side semantics. Broker
funds/margin, directional validation, cancellation, revision and mapping checks remain
authoritative.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.mt5_execution_day26 import Day26ExecutionError, _SignalInput
from app.mt5_read_service_day23 import Day23LiveState, Day23Mt5ReadService, Day23ReadError
from app.paper_critical_execution import PaperCriticalExecutionService, _CriticalSignal
from app.risk_sizing_day24 import Day24RiskSizingResult

logger = logging.getLogger(__name__)

DEFAULT_PAPER_MAX_SIGNAL_AGE_SECONDS = 90.0


class PaperExecutionPriorityService(PaperCriticalExecutionService):
    """DEMO-only policy: execute fresh signals with one total signal risk budget."""

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
        """Use the broker's executable quote for a fresh market signal.

        The provider price remains evidence/context, but it is not used as a synthetic
        order price. A market order has no requested open price in MetaAPI; the broker
        fills it at the available market price. Time freshness is the anti-backlog
        safety net instead of an exact-price equality check.
        """
        del owner_user_id, day23
        self._assert_signal_recent(signal)
        try:
            executable = Decimal(
                str(Day23Mt5ReadService.executable_price(initial_state, signal.side))
            )
        except Day23ReadError as exc:
            raise Day26ExecutionError(exc.code) from exc
        return executable, initial_state

    def _validate_entry_timing(
        self,
        entries,
        side: str,
        current: Decimal,
    ) -> None:
        """Do not reject a fresh market layer merely because its quote moved.

        Literal pending orders are different: a BUY LIMIT must still be below market,
        a SELL LIMIT above market, etc. Those broker semantics remain unchanged.
        """
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

    def _size_signal(
        self,
        *,
        signal: _SignalInput,
        execution_entry: Decimal,
        balance: float,
        price_loss_tick_value: float | None,
        specification: dict[str, object],
        risk_percent,
        double_lot_approved: bool,
    ) -> Day24RiskSizingResult:
        """Split the configured risk budget across every TP/runner leg in DEMO.

        Day24's historical contract treats ``risk_percent`` as a per-position budget.
        Feeding it a pro-rata balance makes its aggregate budget equal the configured
        percentage of the real balance while preserving all broker volume rounding.
        """
        position_count = signal.position_count
        if position_count <= 0:
            raise Day26ExecutionError("position_count_invalid")
        per_leg_balance = Decimal(str(balance)) / Decimal(position_count)
        return super()._size_signal(
            signal=signal,
            execution_entry=execution_entry,
            balance=float(per_leg_balance),
            price_loss_tick_value=price_loss_tick_value,
            specification=specification,
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )

    @staticmethod
    def _assert_layer_risk_cap(
        *,
        real_balance: Decimal,
        risk_percent: Decimal,
        double_applied: bool,
        sizings: tuple[Day24RiskSizingResult, ...],
        tp_count: int,
    ) -> None:
        """Treat configured risk as sizing guidance, not a DEMO placement veto.

        The broker minimum-volume rule is real and MetaAPI exposes it in the symbol
        specification. If 0.01 lots make realised paper risk exceed the selected risk
        percentage, record the overrun and continue. Broker margin/funds checks still
        decide whether the account can actually place the requested positions.
        """
        if tp_count <= 0:
            raise Day26ExecutionError("position_count_invalid")
        multiplier = Decimal("2") if double_applied else Decimal("1")
        total_signal_guide = real_balance * risk_percent * multiplier / Decimal("100")
        actual_one_slot_per_entry = sum(
            (item.actual_risk_per_position for item in sizings), Decimal("0")
        )
        if actual_one_slot_per_entry > total_signal_guide:
            logger.warning(
                "DEMO minimum-lot risk exceeds total signal sizing guide; continuing "
                "observed=%s guide=%s",
                actual_one_slot_per_entry,
                total_signal_guide,
            )


__all__ = ["PaperExecutionPriorityService"]
