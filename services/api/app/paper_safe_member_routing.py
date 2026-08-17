"""Keep paper-only critical execution structures away from LIVE member accounts.

The Owner paper boundary now understands exact pending orders and explicit entry
layers. Day 38 member fan-out is intentionally not promoted with it. These wrappers
preserve the existing member target/audit machinery while returning an explicit
paper-only skip before any LIVE broker service is called.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import text

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

_LAYER = re.compile(r"\b(?:SECOND|2ND|THIRD|3RD|FOURTH|4TH|FIFTH|5TH)\s+ENTRY\b", re.IGNORECASE)
_CRITICAL_TARGET = re.compile(r"(?:^|_)(?:ENTRY|LAYER|PARTIAL)(?:_|$)", re.IGNORECASE)


class PaperSafeMemberDistribution(Day38MultiUserDistributionService):
    """Block pending/layered canonical signals before LIVE member execution."""

    async def distribute(self, *, signal_id: UUID) -> Day38DistributionResult:
        if not self._critical_signal(signal_id):
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
                error_code="critical_structure_paper_only",
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

    def _critical_signal(self, signal_id: UUID) -> bool:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT order_type, original_text
                    FROM signals
                    WHERE id=:signal_id
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
        if row is None:
            # Missing canonical identity must never be allowed to fall through to a
            # LIVE mutation just because this guard could not classify it.
            return True
        if str(row["order_type"] or "").strip().lower() == "pending":
            return True
        return _LAYER.search(str(row["original_text"] or "")) is not None


class PaperSafeMemberManagement(Day38MultiUserManagementService):
    """Block layer/true-partial management from the existing LIVE member engine."""

    async def distribute(
        self,
        *,
        signal_id: UUID,
        lifecycle_event_id: UUID,
    ) -> Day38ManagementDistributionResult:
        if not self._critical_event(lifecycle_event_id):
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
                error_code="critical_management_paper_only",
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

    def _critical_event(self, lifecycle_event_id: UUID) -> bool:
        with self._session_factory() as session:
            aggregate = session.execute(
                text(
                    """
                    SELECT aggregate_result
                    FROM signal_lifecycle_events
                    WHERE id=:event_id
                    LIMIT 1
                    """
                ),
                {"event_id": lifecycle_event_id},
            ).scalar_one_or_none()
        if not isinstance(aggregate, dict):
            return True
        revised = aggregate.get("revised_instruction")
        if not isinstance(revised, dict):
            return True
        actions = revised.get("management_actions")
        if not isinstance(actions, list):
            target = str(revised.get("update_target") or "")
            return _CRITICAL_TARGET.search(target) is not None
        for action in actions:
            if not isinstance(action, dict):
                return True
            target = str(action.get("target") or "")
            if _CRITICAL_TARGET.search(target):
                return True
        return False


__all__ = ["PaperSafeMemberDistribution", "PaperSafeMemberManagement"]
