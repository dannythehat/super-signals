"""Execute still-valid market targets when price crosses an early TP before capture.

A fast provider market signal can arrive after the broker price has already crossed one
or more early take-profits. That must not discard later targets that are still ahead of
market. This adapter keeps only the still-valid targets while preserving their original
provider TP indexes and provider-specific risk allocation.

The rule is deliberately limited to exact-price market signals. Pending orders and entry
zones retain their existing literal/fail-closed policy.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
import logging
from uuid import UUID, uuid4

from sqlalchemy import text

from app.execution_capture_reliability import (
    CaptureReliableCanonicalTradingExecutionService,
    CaptureReliableMemberTradingExecutionService,
)
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    _PlannedPosition,
    _SignalInput,
    target_risk_percent,
)
from app.mt5_read_service_day23 import Day23LiveState, Day23Mt5ReadService, Day23ReadError
from app.provider_risk_policy import provider_risk_profile
from app.risk_sizing_day24 import Day24RiskSizingResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ExecutionContext:
    selected_risk: Decimal
    signal_id: UUID | None = None
    target_indices: tuple[int, ...] = ()
    original_position_count: int = 0
    sizing_cursor: int = 0


_EXECUTION_CONTEXT: ContextVar[_ExecutionContext | None] = ContextVar(
    "super_signals_graceful_market_target_context",
    default=None,
)


def remaining_market_targets(
    *,
    side: str,
    executable: Decimal,
    stop_loss: Decimal,
    take_profits: tuple[Decimal, ...],
) -> tuple[tuple[int, ...], tuple[Decimal, ...]]:
    """Return original TP indexes/values that remain beyond the executable price."""
    normalized_side = side.strip().upper()
    if not take_profits:
        return (), ()

    if normalized_side == "BUY":
        if stop_loss >= executable:
            return (), ()
        if any(right <= left for left, right in zip(take_profits, take_profits[1:])):
            return (), ()
        kept = tuple(
            (index, target)
            for index, target in enumerate(take_profits, start=1)
            if target > executable
        )
    elif normalized_side == "SELL":
        if stop_loss <= executable:
            return (), ()
        if any(right >= left for left, right in zip(take_profits, take_profits[1:])):
            return (), ()
        kept = tuple(
            (index, target)
            for index, target in enumerate(take_profits, start=1)
            if target < executable
        )
    else:
        return (), ()

    return (
        tuple(index for index, _ in kept),
        tuple(target for _, target in kept),
    )


def original_target_risk(
    *,
    source_name: str,
    side: str,
    original_position_count: int,
    original_target_index: int,
    selected_risk: Decimal,
) -> Decimal:
    """Resolve risk using the provider's original TP number, never the compacted slot."""
    profile = provider_risk_profile(
        source_name=source_name,
        side=side,
        position_count=original_position_count,
    )
    if profile is not None:
        return Decimal(str(profile[original_target_index - 1]))
    return target_risk_percent(selected_risk, original_target_index)


class _GracefulCrossedMarketTargetMixin:
    async def execute_owner_demo_signal(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        risk_percent,
        double_lot_approved: bool,
    ):
        token = _EXECUTION_CONTEXT.set(
            _ExecutionContext(selected_risk=Decimal(str(risk_percent)))
        )
        try:
            return await super().execute_owner_demo_signal(
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                risk_percent=risk_percent,
                double_lot_approved=double_lot_approved,
            )
        finally:
            _EXECUTION_CONTEXT.reset(token)

    async def _resolve_entry(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        day23: Day23Mt5ReadService,
        initial_state: Day23LiveState,
    ) -> tuple[Decimal, Day23LiveState]:
        # Bare GOLD NOW is handled by the canonical service, and zones/pending-shaped
        # structures retain their existing policy. This recovery is for exact market
        # entries only.
        if (
            not signal.take_profits
            or signal.is_zone
            or (signal.entry_low == 0 and signal.entry_high == 0)
        ):
            return await super()._resolve_entry(
                owner_user_id=owner_user_id,
                signal=signal,
                day23=day23,
                initial_state=initial_state,
            )

        self._assert_signal_recent(signal)
        try:
            executable = Decimal(
                str(Day23Mt5ReadService.executable_price(initial_state, signal.side))
            )
        except Day23ReadError as exc:
            raise Day26ExecutionError(exc.code) from exc

        original_targets = tuple(signal.take_profits)
        kept_indices, kept_targets = remaining_market_targets(
            side=signal.side,
            executable=executable,
            stop_loss=signal.stop_loss,
            take_profits=original_targets,
        )

        if len(kept_targets) == len(original_targets):
            return await super()._resolve_entry(
                owner_user_id=owner_user_id,
                signal=signal,
                day23=day23,
                initial_state=initial_state,
            )
        if not kept_targets and not signal.has_open_runner:
            raise Day26ExecutionError("strict_directional_validation_failed")

        original_position_count = len(original_targets) + (1 if signal.has_open_runner else 0)
        target_indices = kept_indices
        if signal.has_open_runner:
            target_indices = target_indices + (original_position_count,)

        object.__setattr__(signal, "take_profits", kept_targets)

        context = _EXECUTION_CONTEXT.get()
        if context is not None:
            context.signal_id = signal.signal_id
            context.target_indices = target_indices
            context.original_position_count = original_position_count
            context.sizing_cursor = 0

        logger.warning(
            "Fast market move crossed early TP(s); executing remaining targets "
            "signal=%s side=%s executable=%s original_targets=%s remaining_indexes=%s",
            signal.signal_id,
            signal.side,
            executable,
            tuple(str(value) for value in original_targets),
            target_indices,
        )
        return executable, initial_state

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
        context = _EXECUTION_CONTEXT.get()
        if (
            context is not None
            and context.signal_id == signal.signal_id
            and context.target_indices
            and context.sizing_cursor < len(context.target_indices)
        ):
            original_index = context.target_indices[context.sizing_cursor]
            context.sizing_cursor += 1
            risk_percent = original_target_risk(
                source_name=self._source_name(signal.signal_id),
                side=signal.side,
                original_position_count=context.original_position_count,
                original_target_index=original_index,
                selected_risk=context.selected_risk,
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

    def _create_planned_positions(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        sizings: dict[int, Day24RiskSizingResult],
        execution_entry: Decimal,
    ) -> tuple[_PlannedPosition, ...]:
        context = _EXECUTION_CONTEXT.get()
        if (
            context is None
            or context.signal_id != signal.signal_id
            or not context.target_indices
        ):
            return super()._create_planned_positions(
                owner_user_id=owner_user_id,
                signal=signal,
                sizings=sizings,
                execution_entry=execution_entry,
            )

        targets: list[Decimal | None] = list(signal.take_profits)
        if signal.has_open_runner:
            targets.append(None)
        if len(targets) != len(context.target_indices):
            raise Day26ExecutionError("position_count_invalid")

        planned: list[_PlannedPosition] = []
        with self._session_factory() as session:
            for slot_index, (tp_index, take_profit) in enumerate(
                zip(context.target_indices, targets),
                start=1,
            ):
                sizing = sizings[slot_index]
                local_id = uuid4()
                client_id = f"SS_{local_id.hex[:12]}_{tp_index}"
                session.execute(
                    text(
                        """
                        INSERT INTO positions (
                            id, signal_id, user_id, tp_index, take_profit,
                            planned_risk_percent, volume, stop_loss,
                            broker_client_id, status, entry_price
                        ) VALUES (
                            :id, :signal_id, :user_id, :tp_index, :take_profit,
                            :risk_percent, :volume, :stop_loss,
                            :client_id, 'planned', :entry_price
                        )
                        """
                    ),
                    {
                        "id": local_id,
                        "signal_id": signal.signal_id,
                        "user_id": owner_user_id,
                        "tp_index": tp_index,
                        "take_profit": take_profit,
                        "risk_percent": sizing.effective_risk_percent,
                        "volume": sizing.volume,
                        "stop_loss": signal.stop_loss,
                        "client_id": client_id,
                        "entry_price": execution_entry,
                    },
                )
                planned.append(
                    _PlannedPosition(
                        local_position_id=local_id,
                        tp_index=tp_index,
                        take_profit=take_profit,
                        client_id=client_id,
                        sizing=sizing,
                    )
                )
            session.commit()
        return tuple(planned)


class GracefulCaptureReliableCanonicalTradingExecutionService(
    _GracefulCrossedMarketTargetMixin,
    CaptureReliableCanonicalTradingExecutionService,
):
    """Owner demo execution with fast-market TP preservation."""


class GracefulCaptureReliableMemberTradingExecutionService(
    _GracefulCrossedMarketTargetMixin,
    CaptureReliableMemberTradingExecutionService,
):
    """Member execution with the same fast-market TP preservation."""


__all__ = [
    "GracefulCaptureReliableCanonicalTradingExecutionService",
    "GracefulCaptureReliableMemberTradingExecutionService",
    "original_target_risk",
    "remaining_market_targets",
]
