"""Validate Owner DEMO market fills against provider SL/TP at the fresh broker price."""

from __future__ import annotations

from app.market_at_best_price_override import _live_directionally_valid
from app.mt5_execution_day26 import Day26ExecutionError


def install_paper_live_entry_guard() -> None:
    from app.paper_execution_priority import PaperExecutionPriorityService

    original = PaperExecutionPriorityService._resolve_entry
    if getattr(original, "_paper_live_entry_guard_installed", False):
        return

    async def wrapped(self, **kwargs):
        executable, state = await original(self, **kwargs)
        signal = kwargs["signal"]
        if not _live_directionally_valid(
            side=signal.side,
            entry=executable,
            stop_loss=signal.stop_loss,
            take_profits=signal.take_profits,
        ):
            raise Day26ExecutionError("strict_directional_validation_failed")
        return executable, state

    wrapped._paper_live_entry_guard_installed = True  # type: ignore[attr-defined]
    PaperExecutionPriorityService._resolve_entry = wrapped


__all__ = ["install_paper_live_entry_guard"]
