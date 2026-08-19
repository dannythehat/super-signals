"""Canonical shared paper/future-LIVE execution policy.

Provider entry layers and TP/runner targets are represented with the minimum faithful set
of atomic broker positions. Risk is per provider section: selected 1% means every
leg/section carries 1% (or the explicitly approved effective risk), never an aggregate
account pot and never divided across the signal.

There is deliberately no local balance/free-margin/capacity veto. Day24 may use the
fresh broker balance to calculate the monetary amount represented by 1% risk, but local
code does not decide whether the account can afford the requested provider trade. Every
valid broker mutation is submitted; Vantage/MT5 is the sole authority for an actual
funds/margin rejection.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text

from app.critical_entry_policy import CriticalEntry, parse_critical_entries
from app.mt5_execution_day26 import Day26ExecutionError, Day26Mt5ExecutionService, _SignalInput
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.paper_critical_execution import PaperCriticalExecutionService, _Planned
from app.paper_execution_priority import PaperExecutionPriorityService
from app.risk_sizing_day24 import Day24RiskSizingResult


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
    """Shared executor with full selected risk per provider section."""

    async def execute_owner_demo_signal(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        risk_percent,
        double_lot_approved: bool,
    ):
        """Route one canonical signal by literal provider broker structure."""
        with self._session_factory() as session:
            shape = session.execute(
                text(
                    """
                    SELECT order_type,entry_low,entry_high
                    FROM signals WHERE id=:signal_id LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
        if shape is None:
            raise Day26ExecutionError("signal_not_found")

        no_entry_market = (
            str(shape["order_type"] or "").lower() == "market"
            and shape["entry_low"] is None
            and shape["entry_high"] is None
        )
        if no_entry_market:
            section_count = 1
            entries: tuple[CriticalEntry, ...] = ()
            is_critical = False
        else:
            critical = self._load_critical_signal(signal_id)
            try:
                entries = parse_critical_entries(
                    critical.original_text,
                    side=critical.base.side,
                    entry_low=critical.base.entry_low,
                    entry_high=critical.base.entry_high,
                )
            except ValueError as exc:
                raise Day26ExecutionError(str(exc)) from exc
            section_count = max(1, len(entries))
            is_critical = critical.broad_order_type == "pending" or len(entries) > 1

        context_token = _full_risk_section_count.set(section_count)
        try:
            if is_critical:
                return await PaperCriticalExecutionService.execute_owner_demo_signal(
                    self,
                    owner_user_id=owner_user_id,
                    signal_id=signal_id,
                    risk_percent=risk_percent,
                    double_lot_approved=double_lot_approved,
                )
            return await AtomicDay26Mt5ExecutionService.execute_owner_demo_signal(
                self,
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                risk_percent=risk_percent,
                double_lot_approved=double_lot_approved,
            )
        finally:
            _full_risk_section_count.reset(context_token)

    @staticmethod
    def _required_decimal(value: object, code: str) -> Decimal:
        # NULL entry is deliberate provider truth for a complete market signal with no
        # explicit entry. Zero is internal execution sentinel only and is never written
        # back to the Signal ledger.
        if code == "signal_entry_invalid" and value is None:
            return Decimal("0")
        return Day26Mt5ExecutionService._required_decimal(value, code)

    @staticmethod
    def _directionally_valid(
        *,
        side: str,
        entry_low: Decimal,
        entry_high: Decimal,
        stop_loss: Decimal,
        take_profits: tuple[Decimal, ...],
    ) -> bool:
        if entry_low == 0 and entry_high == 0:
            if stop_loss <= 0 or not take_profits:
                return False
            if side == "BUY":
                return all(right > left for left, right in zip(take_profits, take_profits[1:]))
            if side == "SELL":
                return all(right < left for left, right in zip(take_profits, take_profits[1:]))
            return False
        return Day26Mt5ExecutionService._directionally_valid(
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
        )

    def _provider_zone(self, signal_id: UUID) -> tuple[Decimal, Decimal]:
        with self._session_factory() as session:
            row = session.execute(
                text("SELECT entry_low,entry_high FROM signals WHERE id=:signal_id LIMIT 1"),
                {"signal_id": signal_id},
            ).mappings().first()
        if row is not None and row["entry_low"] is None and row["entry_high"] is None:
            return Decimal("0"), Decimal("0")
        return super()._provider_zone(signal_id)

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
        section_count = max(1, _full_risk_section_count.get())
        reconstructed = Decimal(str(balance)) * Decimal(section_count)
        if section_count > 1:
            reconstructed = reconstructed.quantize(Decimal("0.01"))
        return super()._size_signal(
            signal=signal,
            execution_entry=execution_entry,
            balance=float(reconstructed),
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
        """Validate local shape only; broker funds/margin authority is MT5 itself."""
        del token, account_id, region, symbol, side, free_margin, entries
        if not sizings:
            raise Day26ExecutionError("position_count_invalid")
        target_count = int(next(iter(sizings.values())).position_count)
        if target_count <= 0:
            raise Day26ExecutionError("position_count_invalid")

    def _record_critical_order(
        self,
        item: _Planned,
        order_id: str,
        position_id: str | None,
    ) -> None:
        status = (
            "open"
            if position_id
            else "pending"
            if item.entry.order_type != "market"
            else "planned"
        )
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET broker_order_id=:order_id,
                        broker_position_id=COALESCE(:position_id,broker_position_id),
                        status=:status,
                        opened_at=CASE WHEN :is_open
                                       THEN COALESCE(opened_at,now())
                                       ELSE opened_at END,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": item.local_id,
                    "order_id": order_id,
                    "position_id": position_id,
                    "status": status,
                    "is_open": status == "open",
                },
            )
            session.commit()

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
                client_id = f"SS_{local_id.hex[:12]}_E{entry.entry_index}T{allocation.tp_index}"
                local_entry = market_entry if entry.order_type == "market" else entry.price
                session.execute(
                    text(
                        """
                        INSERT INTO positions (
                            id,signal_id,user_id,entry_index,entry_order_type,
                            tp_index,take_profit,planned_risk_percent,volume,
                            stop_loss,broker_client_id,status,entry_price
                        ) VALUES (
                            :id,:signal_id,:user_id,:entry_index,:entry_order_type,
                            :tp_index,:take_profit,:risk_percent,:volume,
                            :stop_loss,:client_id,'planned',:entry_price
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
                        "risk_percent": sizing.effective_risk_percent,
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
