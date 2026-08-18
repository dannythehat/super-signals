"""Global LIVE execution switch without trading-policy divergence.

Paper testing remains the active production mode. Ordinary member LIVE broker mutation
is therefore disabled by default and requires the explicit environment switch
``SUPER_SIGNALS_LIVE_EXECUTION_ENABLED=true``.

Crucially, this wrapper does NOT inspect signal structure. When LIVE is enabled every
canonical trade and management instruction is delegated to the same paper-tested engine,
including layered entries, pending orders, partials and layer-scoped management.
"""

from __future__ import annotations

import os
from uuid import UUID

from app.multi_user_distribution_day38 import (
    Day38DistributionResult,
    Day38MultiUserDistributionService,
    Day38UserDistributionOutcome,
)
from app.multi_user_management_day38 import (
    Day38ManagementDistributionResult,
    Day38MultiUserManagementService,
    Day38UserManagementOutcome,
)


def live_execution_enabled() -> bool:
    return os.getenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class PaperSafeMemberDistribution(Day38MultiUserDistributionService):
    """Disable all LIVE member entries until the explicit global switch is flipped."""

    async def distribute(self, *, signal_id: UUID) -> Day38DistributionResult:
        if live_execution_enabled():
            return await super().distribute(signal_id=signal_id)

        targets = self._targets()
        outcomes = tuple(
            Day38UserDistributionOutcome(
                user_id=target.user_id,
                outcome="skipped",
                risk_percent=target.risk_percent,
                allow_double_lot=target.allow_double_lot,
                position_count=0,
                volume_per_position=(),
                error_code="live_execution_disabled",
            )
            for target in targets
        )
        for outcome in outcomes:
            self._audit_user(signal_id=signal_id, outcome=outcome)
        result = Day38DistributionResult(
            signal_id=signal_id,
            target_count=len(targets),
            executed_count=0,
            skipped_count=len(outcomes),
            outcomes=outcomes,
        )
        self._audit_summary(result)
        return result


class PaperSafeMemberManagement(Day38MultiUserManagementService):
    """Disable all LIVE member management until the same global switch is flipped."""

    async def distribute(
        self,
        *,
        signal_id: UUID,
        lifecycle_event_id: UUID,
    ) -> Day38ManagementDistributionResult:
        if live_execution_enabled():
            return await super().distribute(
                signal_id=signal_id,
                lifecycle_event_id=lifecycle_event_id,
            )

        targets = self._targets(signal_id)
        outcomes = tuple(
            Day38UserManagementOutcome(
                user_id=user_id,
                outcome="skipped",
                broker_actions_sent=0,
                positions_closed=0,
                positions_modified=0,
                orders_cancelled=0,
                error_code="live_execution_disabled",
            )
            for user_id in targets
        )
        for outcome in outcomes:
            self._audit_user(signal_id, lifecycle_event_id, outcome)
        result = Day38ManagementDistributionResult(
            signal_id=signal_id,
            lifecycle_event_id=lifecycle_event_id,
            target_count=len(targets),
            managed_count=0,
            skipped_count=len(outcomes),
            already_applied_count=0,
            outcomes=outcomes,
        )
        self._audit_summary(result)
        return result


__all__ = [
    "PaperSafeMemberDistribution",
    "PaperSafeMemberManagement",
    "live_execution_enabled",
]
