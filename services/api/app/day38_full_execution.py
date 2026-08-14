"""Day 38 canonical router: Owner reference + independent member fan-out.

The canonical Telegram decision is processed once. Owner demo execution remains a
reference path, but member execution never depends on it succeeding first. Each member
is attempted independently through the Day 38 LIVE executor. Provider management is
also fanned out to members that actually hold mapped exposure so enabling Day 38 cannot
create trades that later stop receiving provider instructions.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.day28_full_execution import (
    Day28FullExecutionRouter,
    Day28RouteResult,
    _StoredDecision,
)
from app.models import AuditEvent
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_management_day27 import Day27ManagementError
from app.multi_user_distribution_day38 import Day38MultiUserDistributionService
from app.multi_user_management_day38 import Day38MultiUserManagementService


class Day38FullExecutionRouter(Day28FullExecutionRouter):
    """Keep Day 28 semantics while adding isolated ordinary-user fan-out."""

    def __init__(
        self,
        *,
        member_distribution: Day38MultiUserDistributionService,
        member_management: Day38MultiUserManagementService,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._member_distribution = member_distribution
        self._member_management = member_management

    async def _dispatch_new_trade(
        self,
        stored: _StoredDecision,
        revision_index: int,
    ) -> Day28RouteResult:
        signal_id = self._resolve_signal_id(stored.message_id, revision_index)
        if signal_id is None:
            self._audit_failure(
                entity_id=stored.message_id,
                entity_type="message",
                error_code="day28_signal_not_resolved",
                decision=stored.decision,
                action=stored.action,
            )
            return Day28RouteResult(
                outcome="blocked",
                decision=stored.decision,
                action=stored.action,
                error_code="day28_signal_not_resolved",
                reason="day28_signal_not_resolved",
            )

        lock = self._locks.setdefault(f"signal:{signal_id}", asyncio.Lock())
        async with lock:
            prior = self._prior_new_trade_route(signal_id)
            if prior is not None:
                if prior["outcome"] == "executed":
                    return Day28RouteResult(
                        outcome="already_applied",
                        decision=stored.decision,
                        action=stored.action,
                        signal_id=signal_id,
                        position_count=int(prior["position_count"] or 0),
                        already_applied=True,
                        reason="day38_distribution_already_attempted",
                    )
                return Day28RouteResult(
                    outcome="blocked",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    error_code=str(prior["error_code"] or "day38_distribution_already_attempted"),
                    reason=str(prior["error_code"] or "day38_distribution_already_attempted"),
                )

            owner_position_count = self._position_count(signal_id)
            owner_succeeded = owner_position_count > 0
            owner_error: str | None = None
            owner_double_lot_applied = False
            if not owner_succeeded:
                try:
                    owner_result = await self._execution.execute_owner_demo_signal(
                        owner_user_id=self._owner_user_id,
                        signal_id=signal_id,
                        risk_percent=str(self._risk_percent),
                        double_lot_approved=self._double_lot_approved,
                    )
                    owner_position_count = len(owner_result.positions)
                    owner_double_lot_applied = bool(owner_result.double_lot_applied)
                    owner_succeeded = owner_position_count > 0
                except Day26ExecutionError as exc:
                    owner_error = exc.code

            try:
                member_result = await self._member_distribution.distribute(signal_id=signal_id)
            except Exception:
                # If Owner already executed, a member-orchestration failure must not
                # turn the canonical route into a false "no broker execution" claim.
                member_result = None

            member_executed = member_result.executed_count if member_result is not None else 0
            member_skipped = member_result.skipped_count if member_result is not None else 0
            success = owner_succeeded or member_executed > 0
            canonical_position_count = owner_position_count
            if canonical_position_count == 0 and member_result is not None:
                canonical_position_count = max(
                    (item.position_count for item in member_result.outcomes if item.outcome == "executed"),
                    default=0,
                )

            if success:
                self._audit_success(
                    entity_id=signal_id,
                    entity_type="signal",
                    payload={
                        "route": "new_trade",
                        "source_revision_index": revision_index,
                        "risk_percent": str(self._risk_percent),
                        "double_lot_approved": self._double_lot_approved,
                        "double_lot_applied": owner_double_lot_applied,
                        "position_count": canonical_position_count,
                        "automatic_execution": True,
                        "day38_multi_user": True,
                        "owner_reference_executed": owner_succeeded,
                        "owner_reference_error_code": owner_error,
                        "member_target_count": (
                            member_result.target_count if member_result is not None else None
                        ),
                        "member_executed_count": member_executed,
                        "member_skipped_count": member_skipped,
                        "owner_failure_blocked_members": False,
                    },
                )
                self._audit_day38_route(
                    signal_id=signal_id,
                    outcome="executed",
                    position_count=canonical_position_count,
                    error_code=None,
                )
                return Day28RouteResult(
                    outcome="executed",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    position_count=canonical_position_count,
                    reason="day38_multi_user_trade_executed",
                )

            error_code = owner_error or "day38_no_user_execution_succeeded"
            self._audit_failure(
                entity_id=signal_id,
                entity_type="signal",
                error_code=error_code,
                decision=stored.decision,
                action=stored.action,
                extra={
                    "day38_multi_user": True,
                    "member_target_count": (
                        member_result.target_count if member_result is not None else None
                    ),
                    "member_executed_count": member_executed,
                    "member_skipped_count": member_skipped,
                    "owner_failure_blocked_members": False,
                },
            )
            self._audit_day38_route(
                signal_id=signal_id,
                outcome="blocked",
                position_count=0,
                error_code=error_code,
            )
            return Day28RouteResult(
                outcome="blocked",
                decision=stored.decision,
                action=stored.action,
                signal_id=signal_id,
                error_code=error_code,
                reason=error_code,
            )

    async def _dispatch_management(
        self,
        stored: _StoredDecision,
        revision_index: int,
    ) -> Day28RouteResult:
        lifecycle_event_id, signal_id = self._resolve_lifecycle_event(
            stored.message_id,
            revision_index,
        )
        if lifecycle_event_id is None or signal_id is None:
            self._audit_failure(
                entity_id=stored.message_id,
                entity_type="message",
                error_code="day28_lifecycle_event_not_resolved",
                decision=stored.decision,
                action=stored.action,
            )
            return Day28RouteResult(
                outcome="blocked",
                decision=stored.decision,
                action=stored.action,
                error_code="day28_lifecycle_event_not_resolved",
                reason="day28_lifecycle_event_not_resolved",
            )

        lock = self._locks.setdefault(f"event:{lifecycle_event_id}", asyncio.Lock())
        async with lock:
            owner_has_positions = self._position_count(signal_id) > 0
            owner_result = None
            owner_error: str | None = None
            if owner_has_positions:
                try:
                    owner_result = await self._management.execute_owner_demo_event(
                        owner_user_id=self._owner_user_id,
                        lifecycle_event_id=lifecycle_event_id,
                    )
                except Day27ManagementError as exc:
                    owner_error = exc.code

            try:
                member_result = await self._member_management.distribute(
                    signal_id=signal_id,
                    lifecycle_event_id=lifecycle_event_id,
                )
            except Exception:
                member_result = None

            # A provider can keep publishing management for a setup that Super Signals
            # never opened (for example an entry that was outside the literal zone).
            # With zero Owner positions and zero member targets there is nothing to
            # mutate. That is an expected not-applicable update, not an execution
            # failure. Do not produce a scary broker-route warning or invent a retry.
            # If target exposure exists, or member routing itself was unavailable,
            # the normal failure path below remains authoritative.
            no_exposure_anywhere = (
                not owner_has_positions
                and member_result is not None
                and member_result.target_count == 0
            )
            if no_exposure_anywhere:
                self._audit_success(
                    entity_id=signal_id,
                    entity_type="signal",
                    payload={
                        "route": "trade_update",
                        "source_revision_index": revision_index,
                        "lifecycle_event_id": str(lifecycle_event_id),
                        "broker_actions_sent": 0,
                        "automatic_execution": True,
                        "day38_multi_user": True,
                        "management_applicable": False,
                        "no_mapped_exposure": True,
                        "owner_reference_managed": False,
                        "owner_reference_error_code": None,
                        "member_target_count": 0,
                        "member_managed_count": 0,
                        "member_skipped_count": 0,
                        "owner_failure_blocked_members": False,
                    },
                )
                return Day28RouteResult(
                    outcome="ignored",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    lifecycle_event_id=lifecycle_event_id,
                    broker_actions_sent=0,
                    reason="day38_management_not_applicable_no_positions",
                )

            member_success = (
                member_result.any_management_succeeded if member_result is not None else False
            )
            owner_success = owner_result is not None
            if not owner_success and not member_success:
                error_code = owner_error or "day38_no_user_management_succeeded"
                self._audit_failure(
                    entity_id=signal_id,
                    entity_type="signal",
                    error_code=error_code,
                    decision=stored.decision,
                    action=stored.action,
                    extra={
                        "lifecycle_event_id": str(lifecycle_event_id),
                        "day38_multi_user": True,
                        "owner_failure_blocked_members": False,
                    },
                )
                return Day28RouteResult(
                    outcome="blocked",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    lifecycle_event_id=lifecycle_event_id,
                    error_code=error_code,
                    reason=error_code,
                )

            broker_actions = owner_result.broker_actions_sent if owner_result is not None else 0
            already_applied = bool(owner_result is not None and owner_result.already_applied)
            if member_result is not None:
                broker_actions += sum(item.broker_actions_sent for item in member_result.outcomes)
                if member_result.target_count:
                    already_applied = already_applied and (
                        member_result.already_applied_count == member_result.target_count
                    )

            self._audit_success(
                entity_id=signal_id,
                entity_type="signal",
                payload={
                    "route": "trade_update",
                    "source_revision_index": revision_index,
                    "lifecycle_event_id": str(lifecycle_event_id),
                    "broker_actions_sent": broker_actions,
                    "automatic_execution": True,
                    "day38_multi_user": True,
                    "owner_reference_managed": owner_success,
                    "owner_reference_error_code": owner_error,
                    "member_target_count": (
                        member_result.target_count if member_result is not None else None
                    ),
                    "member_managed_count": (
                        member_result.managed_count if member_result is not None else 0
                    ),
                    "member_skipped_count": (
                        member_result.skipped_count if member_result is not None else 0
                    ),
                    "owner_failure_blocked_members": False,
                },
            )
            return Day28RouteResult(
                outcome=("already_applied" if already_applied else "managed"),
                decision=stored.decision,
                action=stored.action,
                signal_id=signal_id,
                lifecycle_event_id=lifecycle_event_id,
                broker_actions_sent=broker_actions,
                already_applied=already_applied,
                reason=(
                    "day38_management_already_applied"
                    if already_applied
                    else "day38_multi_user_management_applied"
                ),
            )

    def _prior_new_trade_route(self, signal_id: UUID) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT payload
                    FROM audit_events
                    WHERE event_type='mt5.day38_route_new_trade'
                      AND entity_type='signal'
                      AND entity_id=:signal_id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).scalar_one_or_none()
        return row if isinstance(row, dict) else None

    def _audit_day38_route(
        self,
        *,
        signal_id: UUID,
        outcome: str,
        position_count: int,
        error_code: str | None,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type="mt5.day38_route_new_trade",
                    entity_type="signal",
                    entity_id=signal_id,
                    payload={
                        "outcome": outcome,
                        "position_count": position_count,
                        "error_code": error_code,
                        "automatic_retry": False,
                    },
                )
            )
            session.commit()


__all__ = ["Day38FullExecutionRouter"]
