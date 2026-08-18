"""Keep broker account truth current even when there are no unsettled positions.

Day 34 originally skipped performance sync entirely while the account was flat. That
made non-trade balance operations invisible until a later trade/history refresh. This
wrapper keeps the existing 15-second settlement behaviour for active trades and performs
a read-only account-history reconciliation at most once per minute while flat.
"""

from __future__ import annotations

import time

from app.broker_settlement_day34 import (
    Day34BrokerSettlementManager,
    Day34SettlementPollResult,
)
from app.performance_ledger_day33 import Day33LedgerError


_INSTALLED = False
_ORIGINAL_POLL_ONCE = Day34BrokerSettlementManager.poll_once
_FLAT_SYNC_SECONDS = 60.0


async def _poll_once_with_account_truth(
    self: Day34BrokerSettlementManager,
) -> Day34SettlementPollResult:
    if self._has_unsettled_mapped_positions():
        return await _ORIGINAL_POLL_ONCE(self)

    now = time.monotonic()
    last = float(getattr(self, "_account_truth_last_sync", 0.0) or 0.0)
    if last and now - last < _FLAT_SYNC_SECONDS:
        return Day34SettlementPollResult(
            synced=False,
            positions_reconciled=0,
            position_events_created=0,
            signal_results_created=0,
            reason="flat_account_truth_sync_not_due",
        )

    try:
        await self._performance.sync_user(self._reference_user_id)
    except Day33LedgerError as exc:
        self._audit_poll_failure(exc.code, retryable=exc.retryable)
        return Day34SettlementPollResult(
            synced=False,
            positions_reconciled=0,
            position_events_created=0,
            signal_results_created=0,
            reason=exc.code,
        )

    self._account_truth_last_sync = now
    return Day34SettlementPollResult(
        synced=True,
        positions_reconciled=0,
        position_events_created=0,
        signal_results_created=0,
        reason="flat_account_truth_sync_complete",
    )


def install_account_truth_poll_override() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    Day34BrokerSettlementManager.poll_once = _poll_once_with_account_truth  # type: ignore[method-assign]
    _INSTALLED = True


__all__ = ["install_account_truth_poll_override"]
