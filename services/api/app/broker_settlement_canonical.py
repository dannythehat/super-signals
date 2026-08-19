"""Canonical broker-led settlement and account-truth polling.

Broker history is authoritative whether trades are currently unsettled or the account is
flat. Active trades keep the normal settlement cadence. While flat, the manager performs
a read-only account/deal sync at most once per minute so balance operations and delayed
broker history cannot leave the app stale.
"""

from __future__ import annotations

import time

from app.broker_settlement_day34 import (
    Day34BrokerSettlementManager,
    Day34SettlementPollResult,
)
from app.performance_ledger_day33 import Day33LedgerError

_FLAT_ACCOUNT_SYNC_SECONDS = 60.0


class CanonicalBrokerSettlementManager(Day34BrokerSettlementManager):
    """One settlement manager for active-position and flat-account broker truth."""

    async def poll_once(self) -> Day34SettlementPollResult:
        if self._has_unsettled_mapped_positions():
            return await super().poll_once()

        now = time.monotonic()
        last = float(getattr(self, "_account_truth_last_sync", 0.0) or 0.0)
        if last and now - last < _FLAT_ACCOUNT_SYNC_SECONDS:
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


__all__ = ["CanonicalBrokerSettlementManager"]
