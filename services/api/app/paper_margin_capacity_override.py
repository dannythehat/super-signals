"""Shared no-balance-veto policy for provider signal evaluation.

The selected risk percentage belongs to each provider section/leg. It is not an
account-wide aggregate exposure budget and it must not be converted into an advisory
free-margin veto that prevents otherwise valid provider signals from reaching MT5.

This override removes the local aggregate margin preflight from the shared
PaperFreshStartExecutionService. Owner DEMO uses that engine now; approved LIVE members
use the same engine when the separate global LIVE switch is eventually enabled. Every
provider leg keeps its independently selected risk, exact provider entry semantics,
side, SL and TP. The real broker remains authoritative for each actual order submission.
"""

from __future__ import annotations

from typing import Any

from app.mt5_execution_day26 import Day26ExecutionError
from app.paper_fresh_start_execution import PaperFreshStartExecutionService

_installed = False


async def _paper_no_balance_veto(
    self: Any,
    *,
    token: str,
    account_id: str,
    region: str,
    symbol: str,
    side: str,
    free_margin: Any,
    entries: tuple[Any, ...],
    sizings: dict[int, Any],
) -> None:
    """Never block a provider setup because of a local aggregate balance calculation."""
    del self, token, account_id, region, symbol, side, free_margin, entries
    if not sizings:
        raise Day26ExecutionError("position_count_invalid")
    target_count = int(next(iter(sizings.values())).position_count)
    if target_count <= 0:
        raise Day26ExecutionError("position_count_invalid")
    # Deliberately no balance/free-margin/margin-calculator gate here. Each actual
    # broker mutation remains authoritative when the order is submitted.
    return None


def install_paper_margin_capacity_override() -> None:
    global _installed
    if _installed:
        return
    PaperFreshStartExecutionService._margin_preflight = _paper_no_balance_veto
    _installed = True


__all__ = ["install_paper_margin_capacity_override"]
