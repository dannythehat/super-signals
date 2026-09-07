from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, TypeVar

TEntry = TypeVar("TEntry")


@dataclass(frozen=True, slots=True)
class LayerAllocation(Generic[TEntry]):
    entry: TEntry
    tp_index: int
    take_profit: Decimal | None


def allocate_entry_targets(
    entries: tuple[TEntry, ...],
    targets: tuple[Decimal | None, ...],
) -> tuple[LayerAllocation[TEntry], ...]:
    """Pure canonical entry/target allocation shared by execution and calibration."""
    if not entries:
        raise ValueError("critical_entry_plan_missing")
    if not targets:
        raise ValueError("position_count_invalid")
    slot_count = max(len(entries), len(targets))
    has_runner = targets[-1] is None
    target_indexes = list(range(1, len(targets) + 1))
    if slot_count > len(targets):
        repeatable = list(range(1, len(targets) if has_runner else len(targets) + 1))
        if not repeatable:
            raise ValueError("position_count_invalid")
        for offset in range(slot_count - len(targets)):
            target_indexes.append(repeatable[offset % len(repeatable)])
    allocations = [
        LayerAllocation(
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
        if getattr(allocations[runner_slot].entry, "entry_index") != getattr(best_entry, "entry_index"):
            best_slot = next(
                index
                for index, item in enumerate(allocations)
                if getattr(item.entry, "entry_index") == getattr(best_entry, "entry_index")
                and item.take_profit is not None
            )
            runner_item = allocations[runner_slot]
            best_item = allocations[best_slot]
            allocations[runner_slot] = LayerAllocation(
                entry=best_item.entry,
                tp_index=runner_item.tp_index,
                take_profit=runner_item.take_profit,
            )
            allocations[best_slot] = LayerAllocation(
                entry=runner_item.entry,
                tp_index=best_item.tp_index,
                take_profit=best_item.take_profit,
            )
    return tuple(allocations)
