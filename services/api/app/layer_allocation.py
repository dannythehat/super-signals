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
    """Allocate exactly one broker leg to every provider TP/runner target.

    Entry sections decide *where* a target leg is placed; they never increase the number
    of risk-bearing legs. With two entries and four targets this therefore returns four
    legs, not eight. If a provider supplies more entry sections than targets, entries are
    consumed only as needed to place those target legs rather than manufacturing extra
    exposure.
    """
    if not entries:
        raise ValueError("critical_entry_plan_missing")
    if not targets:
        raise ValueError("position_count_invalid")

    allocations = [
        LayerAllocation(
            entry=entries[index % len(entries)],
            tp_index=index + 1,
            take_profit=target,
        )
        for index, target in enumerate(targets)
    ]

    # Keep an open runner on the provider's best/latest entry section without changing
    # the total leg count. Swap entry ownership with a numeric target if necessary.
    if targets[-1] is None:
        runner_slot = len(allocations) - 1
        best_entry = entries[-1]
        if getattr(allocations[runner_slot].entry, "entry_index") != getattr(
            best_entry, "entry_index"
        ):
            best_slot = next(
                (
                    index
                    for index, item in enumerate(allocations)
                    if getattr(item.entry, "entry_index")
                    == getattr(best_entry, "entry_index")
                    and item.take_profit is not None
                ),
                None,
            )
            if best_slot is not None:
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
