"""Canonical ordinary-member distribution for the shared trading engine.

Paper and future LIVE use the same trading-policy engine. Ordinary member broker
mutation is disabled by default and becomes eligible only when the single global switch
``SUPER_SIGNALS_LIVE_EXECUTION_ENABLED`` is explicitly enabled.

The switch lives at distribution/account eligibility, never inside trade interpretation,
risk sizing, pending/layer policy or provider management. Each eligible member is an
isolated broker attempt; one account failure never blocks another member.

Existing ``mt5.day38_*`` audit event names are intentionally retained as durable database
compatibility keys until an explicit data migration replaces them. They are not runtime
architecture selectors.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_management_day27 import Day27ManagementError


def live_execution_enabled() -> bool:
    return os.getenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True, slots=True)
class MemberDistributionTarget:
    user_id: UUID
    risk_percent: Decimal
    allow_double_lot: bool


@dataclass(frozen=True, slots=True)
class MemberDistributionOutcome:
    user_id: UUID
    outcome: str
    risk_percent: Decimal
    allow_double_lot: bool
    position_count: int
    volume_per_position: tuple[Decimal, ...]
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MemberDistributionResult:
    signal_id: UUID
    target_count: int
    executed_count: int
    skipped_count: int
    outcomes: tuple[MemberDistributionOutcome, ...]

    @property
    def any_execution_succeeded(self) -> bool:
        return self.executed_count > 0


class MemberDistributionService:
    """Fan one canonical signal to ordinary members behind the single LIVE switch."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        execution_service: Any,
    ) -> None:
        self._session_factory = session_factory
        self._execution = execution_service

    async def distribute(
        self, *, signal_id: UUID, exclude_live: bool = False
    ) -> MemberDistributionResult:
        targets = self._targets()
        if exclude_live or not live_execution_enabled():
            error_code = (
                "probation_live_execution_excluded"
                if exclude_live
                else "live_execution_disabled"
            )
            outcomes = tuple(
                MemberDistributionOutcome(
                    user_id=target.user_id,
                    outcome="skipped",
                    risk_percent=target.risk_percent,
                    allow_double_lot=target.allow_double_lot,
                    position_count=0,
                    volume_per_position=(),
                    error_code=error_code,
                )
                for target in targets
            )
            for outcome in outcomes:
                self._audit_user(signal_id=signal_id, outcome=outcome)
            result = MemberDistributionResult(
                signal_id=signal_id,
                target_count=len(targets),
                executed_count=0,
                skipped_count=len(outcomes),
                outcomes=outcomes,
            )
            self._audit_summary(result)
            return result

        outcomes: list[MemberDistributionOutcome] = []
        for target in targets:
            try:
                execution = await self._execution.execute_live_user_signal(
                    user_id=target.user_id,
                    signal_id=signal_id,
                )
            except Day26ExecutionError as exc:
                outcome = MemberDistributionOutcome(
                    user_id=target.user_id,
                    outcome="skipped",
                    risk_percent=target.risk_percent,
                    allow_double_lot=target.allow_double_lot,
                    position_count=0,
                    volume_per_position=(),
                    error_code=exc.code,
                )
            except Exception:
                outcome = MemberDistributionOutcome(
                    user_id=target.user_id,
                    outcome="skipped",
                    risk_percent=target.risk_percent,
                    allow_double_lot=target.allow_double_lot,
                    position_count=0,
                    volume_per_position=(),
                    error_code="day38_user_execution_unavailable",
                )
            else:
                outcome = MemberDistributionOutcome(
                    user_id=target.user_id,
                    outcome="executed",
                    risk_percent=execution.base_risk_percent,
                    allow_double_lot=target.allow_double_lot,
                    position_count=len(execution.positions),
                    volume_per_position=tuple(
                        position.volume for position in execution.positions
                    ),
                    error_code=None,
                )
            self._audit_user(signal_id=signal_id, outcome=outcome)
            outcomes.append(outcome)

        result = MemberDistributionResult(
            signal_id=signal_id,
            target_count=len(targets),
            executed_count=sum(item.outcome == "executed" for item in outcomes),
            skipped_count=sum(item.outcome == "skipped" for item in outcomes),
            outcomes=tuple(outcomes),
        )
        self._audit_summary(result)
        return result

    def _targets(self) -> tuple[MemberDistributionTarget, ...]:
        """Return active trading users with an active paid or complimentary entitlement."""
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT u.id, utc.risk_percent, utc.allow_double_lot
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN user_trading_controls AS utc ON utc.user_id=u.id
                    LEFT JOIN member_subscriptions AS ms
                      ON ms.user_id=u.id
                     AND ms.status='active'
                     AND ms.active_until > now()
                    LEFT JOIN complimentary_access_grants AS cag
                      ON cag.user_id=u.id
                     AND cag.status='active'
                    WHERE u.status='active'
                      AND utc.trading_status='active'
                      AND (ms.user_id IS NOT NULL OR cag.user_id IS NOT NULL)
                    ORDER BY u.id
                    """
                )
            ).mappings().all()
        return tuple(
            MemberDistributionTarget(
                user_id=UUID(str(row["id"])),
                risk_percent=Decimal(str(row["risk_percent"])),
                allow_double_lot=bool(row["allow_double_lot"]),
            )
            for row in rows
        )

    def _audit_user(
        self,
        *,
        signal_id: UUID,
        outcome: MemberDistributionOutcome,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=outcome.user_id,
                    event_type=(
                        "mt5.day38_distribution_executed"
                        if outcome.outcome == "executed"
                        else "mt5.day38_distribution_skipped"
                    ),
                    entity_type="signal",
                    entity_id=signal_id,
                    payload={
                        "user_id": str(outcome.user_id),
                        "outcome": outcome.outcome,
                        "risk_percent": str(outcome.risk_percent),
                        "allow_double_lot": outcome.allow_double_lot,
                        "position_count": outcome.position_count,
                        "volumes": [str(value) for value in outcome.volume_per_position],
                        "error_code": outcome.error_code,
                        "isolated_user_attempt": True,
                    },
                )
            )
            session.commit()

    def _audit_summary(self, result: MemberDistributionResult) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="mt5.day38_distribution_completed",
                    entity_type="signal",
                    entity_id=result.signal_id,
                    payload={
                        "target_count": result.target_count,
                        "executed_count": result.executed_count,
                        "skipped_count": result.skipped_count,
                        "user_failures_blocked_other_users": False,
                    },
                )
            )
            session.commit()


@dataclass(frozen=True, slots=True)
class MemberManagementOutcome:
    user_id: UUID
    outcome: str
    broker_actions_sent: int
    positions_closed: int
    positions_modified: int
    orders_cancelled: int
    error_code: str | None = None
    already_applied: bool = False


@dataclass(frozen=True, slots=True)
class MemberManagementResult:
    signal_id: UUID
    lifecycle_event_id: UUID
    target_count: int
    managed_count: int
    skipped_count: int
    already_applied_count: int
    outcomes: tuple[MemberManagementOutcome, ...]

    @property
    def any_management_succeeded(self) -> bool:
        return self.managed_count > 0 or self.already_applied_count > 0


class MemberManagementService:
    """Fan provider management to members that actually hold mapped exposure."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        management_service: Any,
    ) -> None:
        self._session_factory = session_factory
        self._management = management_service

    async def distribute(
        self,
        *,
        signal_id: UUID,
        lifecycle_event_id: UUID,
    ) -> MemberManagementResult:
        targets = self._targets(signal_id)
        if not live_execution_enabled():
            outcomes = tuple(
                MemberManagementOutcome(
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
            result = MemberManagementResult(
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

        outcomes: list[MemberManagementOutcome] = []
        for user_id in targets:
            try:
                result = await self._management.execute_owner_demo_event(
                    owner_user_id=user_id,
                    lifecycle_event_id=lifecycle_event_id,
                )
            except Day27ManagementError as exc:
                outcome = MemberManagementOutcome(
                    user_id=user_id,
                    outcome="skipped",
                    broker_actions_sent=0,
                    positions_closed=0,
                    positions_modified=0,
                    orders_cancelled=0,
                    error_code=exc.code,
                )
            except Exception:
                outcome = MemberManagementOutcome(
                    user_id=user_id,
                    outcome="skipped",
                    broker_actions_sent=0,
                    positions_closed=0,
                    positions_modified=0,
                    orders_cancelled=0,
                    error_code="day38_user_management_unavailable",
                )
            else:
                outcome = MemberManagementOutcome(
                    user_id=user_id,
                    outcome=("already_applied" if result.already_applied else "managed"),
                    broker_actions_sent=result.broker_actions_sent,
                    positions_closed=result.positions_closed,
                    positions_modified=result.positions_modified,
                    orders_cancelled=result.orders_cancelled,
                    already_applied=result.already_applied,
                )
            self._audit_user(signal_id, lifecycle_event_id, outcome)
            outcomes.append(outcome)

        result = MemberManagementResult(
            signal_id=signal_id,
            lifecycle_event_id=lifecycle_event_id,
            target_count=len(targets),
            managed_count=sum(item.outcome == "managed" for item in outcomes),
            skipped_count=sum(item.outcome == "skipped" for item in outcomes),
            already_applied_count=sum(
                item.outcome == "already_applied" for item in outcomes
            ),
            outcomes=tuple(outcomes),
        )
        self._audit_summary(result)
        return result

    def _targets(self, signal_id: UUID) -> tuple[UUID, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT p.user_id
                    FROM positions AS p
                    JOIN users AS u ON u.id=p.user_id AND u.status='active'
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    WHERE p.signal_id=:signal_id
                      AND p.status IN ('open','pending')
                    ORDER BY p.user_id
                    """
                ),
                {"signal_id": signal_id},
            ).scalars().all()
        return tuple(UUID(str(value)) for value in rows)

    def _audit_user(
        self,
        signal_id: UUID,
        lifecycle_event_id: UUID,
        outcome: MemberManagementOutcome,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=outcome.user_id,
                    event_type=(
                        "mt5.day38_management_skipped"
                        if outcome.outcome == "skipped"
                        else "mt5.day38_management_applied"
                    ),
                    entity_type="signal",
                    entity_id=signal_id,
                    payload={
                        "lifecycle_event_id": str(lifecycle_event_id),
                        "outcome": outcome.outcome,
                        "broker_actions_sent": outcome.broker_actions_sent,
                        "positions_closed": outcome.positions_closed,
                        "positions_modified": outcome.positions_modified,
                        "orders_cancelled": outcome.orders_cancelled,
                        "error_code": outcome.error_code,
                        "already_applied": outcome.already_applied,
                        "isolated_user_attempt": True,
                    },
                )
            )
            session.commit()

    def _audit_summary(self, result: MemberManagementResult) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="mt5.day38_management_completed",
                    entity_type="signal",
                    entity_id=result.signal_id,
                    payload={
                        "lifecycle_event_id": str(result.lifecycle_event_id),
                        "target_count": result.target_count,
                        "managed_count": result.managed_count,
                        "skipped_count": result.skipped_count,
                        "already_applied_count": result.already_applied_count,
                        "user_failures_blocked_other_users": False,
                    },
                )
            )
            session.commit()


__all__ = [
    "MemberDistributionOutcome",
    "MemberDistributionResult",
    "MemberDistributionService",
    "MemberDistributionTarget",
    "MemberManagementOutcome",
    "MemberManagementResult",
    "MemberManagementService",
    "live_execution_enabled",
]
