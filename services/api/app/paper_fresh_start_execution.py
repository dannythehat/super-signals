"""Fresh-start Owner DEMO execution fixes.

The provider's entry layers and TP/runner targets are both important, but representing
those two dimensions as a Cartesian product creates far too many broker positions on a
small paper account. This service uses the minimum deterministic set of atomic positions
that covers every declared entry layer and every declared target at least once.

Examples:
* TIG: 2 entries x 4 targets -> 4 broker positions, not 8.
* TDC: 6 entries x 4 targets -> 6 broker positions, not 24.

Risk is intentionally per provider section. A selected 1% risk means every atomic
provider section carries 1%; it is never divided across the whole signal. The fresh
broker free-margin check remains the aggregate availability gate.

The final/deepest retracement layer carries the runner when a runner exists. This keeps
layer-management semantics intact while avoiding an artificial Cartesian explosion.
"""

from __future__ import annotations

from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text

from app.critical_entry_policy import CriticalEntry, parse_critical_entries
from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_execution_day26 import Day26ExecutionError, _SignalInput
from app.paper_critical_execution import _Planned
from app.paper_execution_priority import PaperExecutionPriorityService
from app.risk_sizing_day24 import Day24RiskSizingResult


# The historical critical executor divides live balance by the number of entry layers
# before calling _size_signal. Product policy now explicitly says risk is per provider
# section, not per whole signal. Keep this request-local so simultaneous signals cannot
# leak section counts into one another.
_full_risk_section_count: ContextVar[int] = ContextVar(
    "super_signals_full_risk_section_count",
    default=1,
)


@dataclass(frozen=True, slots=True)
class AtomicLayerAllocation:
    entry: CriticalEntry
    tp_index: int
    take_profit: Decimal | None


class PaperFreshStartExecutionService(PaperExecutionPriorityService):
    """Owner DEMO executor with broker-minimum-aware atomic layer allocation."""

    async def execute_owner_demo_signal(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        risk_percent,
        double_lot_approved: bool,
    ):
        """Execute with the selected risk applied independently to every section."""
        section_count = 1
        try:
            critical = self._load_critical_signal(signal_id)
            entries = parse_critical_entries(
                critical.original_text,
                side=critical.base.side,
                entry_low=critical.base.entry_low,
                entry_high=critical.base.entry_high,
            )
            section_count = max(1, len(entries))
        except (Day26ExecutionError, ValueError):
            # Preserve the inherited fail-closed error path. This pre-read exists only
            # to establish the section count; the canonical executor remains authoritative.
            section_count = 1

        context_token = _full_risk_section_count.set(section_count)
        try:
            return await super().execute_owner_demo_signal(
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                risk_percent=risk_percent,
                double_lot_approved=double_lot_approved,
            )
        finally:
            _full_risk_section_count.reset(context_token)

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
        """Undo the legacy layer balance split before deterministic Day24 sizing."""
        section_count = max(1, _full_risk_section_count.get())
        full_balance = float(Decimal(str(balance)) * Decimal(section_count))
        return super()._size_signal(
            signal=signal,
            execution_entry=execution_entry,
            balance=full_balance,
            price_loss_tick_value=price_loss_tick_value,
            specification=specification,
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )

    @staticmethod
    def _allocation_pairs(
        entries: tuple[CriticalEntry, ...],
        targets: tuple[Decimal | None, ...],
    ) -> tuple[AtomicLayerAllocation, ...]:
        if not entries:
            raise Day26ExecutionError("critical_entry_plan_missing")
        if not targets:
            raise Day26ExecutionError("position_count_invalid")

        slot_count = max(len(entries), len(targets))
        has_runner = targets[-1] is None

        # Cover every target once first. Extra slots exist only when there are more
        # entry layers than targets; repeat numeric profit targets, never the runner.
        target_indexes = list(range(1, len(targets) + 1))
        if slot_count > len(targets):
            repeatable = list(range(1, len(targets) if has_runner else len(targets) + 1))
            if not repeatable:
                raise Day26ExecutionError("position_count_invalid")
            for offset in range(slot_count - len(targets)):
                target_indexes.append(repeatable[offset % len(repeatable)])

        allocations = [
            AtomicLayerAllocation(
                entry=entries[index % len(entries)],
                tp_index=target_index,
                take_profit=targets[target_index - 1],
            )
            for index, target_index in enumerate(target_indexes)
        ]

        # Provider layer ordering moves toward the better retracement as entry_index
        # increases. Put the open runner on that final layer. Swap only the entry
        # assignment, preserving the number of atomic positions allocated per layer.
        if has_runner:
            runner_slot = next(
                index for index, item in enumerate(allocations) if item.take_profit is None
            )
            best_entry = entries[-1]
            if allocations[runner_slot].entry.entry_index != best_entry.entry_index:
                best_slot = next(
                    index
                    for index, item in enumerate(allocations)
                    if item.entry.entry_index == best_entry.entry_index
                    and item.take_profit is not None
                )
                runner_item = allocations[runner_slot]
                best_item = allocations[best_slot]
                allocations[runner_slot] = AtomicLayerAllocation(
                    entry=best_item.entry,
                    tp_index=runner_item.tp_index,
                    take_profit=runner_item.take_profit,
                )
                allocations[best_slot] = AtomicLayerAllocation(
                    entry=runner_item.entry,
                    tp_index=best_item.tp_index,
                    take_profit=best_item.take_profit,
                )

        return tuple(allocations)

    async def _margin_preflight(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        symbol: str,
        side: str,
        free_margin: Decimal,
        entries: tuple[CriticalEntry, ...],
        sizings: dict[int, Day24RiskSizingResult],
    ) -> None:
        # Day24 position_count is the number of TP/runner targets. The atomic plan uses
        # max(entry_count, target_count), not entry_count * target_count. Each atomic
        # section is already sized at the full selected risk; this check asks only
        # whether the broker has enough fresh free margin for the resulting set.
        if not sizings:
            raise Day26ExecutionError("position_count_invalid")
        target_count = next(iter(sizings.values())).position_count
        if target_count <= 0:
            raise Day26ExecutionError("position_count_invalid")
        slot_count = max(len(entries), target_count)
        entry_counts = Counter(
            entries[index % len(entries)].entry_index for index in range(slot_count)
        )

        required_total = Decimal("0")
        complete = True
        for entry in entries:
            sizing = sizings[entry.entry_index]
            total_volume = sizing.volume * Decimal(entry_counts[entry.entry_index])
            try:
                required = await self._margin_gateway.calculate_margin(
                    token=token,
                    account_id=account_id,
                    region=region,
                    symbol=symbol,
                    side=side,
                    volume=float(total_volume),
                    open_price=float(entry.price),
                )
                required_total += Decimal(str(required))
            except MetaApiGatewayError:
                # Existing paper behaviour: broker remains the final authority when
                # the advisory margin calculator itself is unavailable.
                complete = False
        if complete and required_total > free_margin:
            raise Day26ExecutionError("insufficient_funds")

    def _create_layered_plans(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        entries: tuple[CriticalEntry, ...],
        sizings: dict[int, Day24RiskSizingResult],
        market_entry: Decimal,
    ) -> tuple[_Planned, ...]:
        targets: list[Decimal | None] = list(signal.take_profits)
        if signal.has_open_runner:
            targets.append(None)
        allocations = self._allocation_pairs(entries, tuple(targets))

        planned: list[_Planned] = []
        with self._session_factory() as session:
            for allocation in allocations:
                entry = allocation.entry
                sizing = sizings[entry.entry_index]
                local_id = uuid4()
                client_id = (
                    f"SS_{local_id.hex[:12]}_E{entry.entry_index}T{allocation.tp_index}"
                )
                local_entry = market_entry if entry.order_type == "market" else entry.price
                # Permanent Owner rule: selected risk is per provider section. If the
                # minimum faithful allocation repeats one entry to cover another TP,
                # that atomic section still carries the full selected risk.
                planned_risk_percent = sizing.effective_risk_percent
                session.execute(
                    text(
                        """
                        INSERT INTO positions (
                            id, signal_id, user_id, entry_index, entry_order_type,
                            tp_index, take_profit, planned_risk_percent, volume,
                            stop_loss, broker_client_id, status, entry_price
                        ) VALUES (
                            :id, :signal_id, :user_id, :entry_index, :entry_order_type,
                            :tp_index, :take_profit, :risk_percent, :volume,
                            :stop_loss, :client_id, 'planned', :entry_price
                        )
                        """
                    ),
                    {
                        "id": local_id,
                        "signal_id": signal.signal_id,
                        "user_id": owner_user_id,
                        "entry_index": entry.entry_index,
                        "entry_order_type": entry.order_type,
                        "tp_index": allocation.tp_index,
                        "take_profit": allocation.take_profit,
                        "risk_percent": planned_risk_percent,
                        "volume": sizing.volume,
                        "stop_loss": signal.stop_loss,
                        "client_id": client_id,
                        "entry_price": local_entry,
                    },
                )
                planned.append(
                    _Planned(
                        local_id=local_id,
                        entry=entry,
                        tp_index=allocation.tp_index,
                        take_profit=allocation.take_profit,
                        client_id=client_id,
                        sizing=sizing,
                    )
                )
            session.commit()
        return tuple(planned)


__all__ = ["AtomicLayerAllocation", "PaperFreshStartExecutionService"]
