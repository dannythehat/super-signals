"""Day 38 independent fan-out of provider lifecycle updates to member positions."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.mt5_management_day27 import Day27ManagementError
from app.mt5_management_day38 import Day38LiveUserManagementService


@dataclass(frozen=True, slots=True)
class Day38UserManagementOutcome:
    user_id: UUID
    outcome: str
    broker_actions_sent: int
    positions_closed: int
    positions_modified: int
    orders_cancelled: int
    error_code: str | None = None
    already_applied: bool = False


@dataclass(frozen=True, slots=True)
class Day38ManagementDistributionResult:
    signal_id: UUID
    lifecycle_event_id: UUID
    target_count: int
    managed_count: int
    skipped_count: int
    already_applied_count: int
    outcomes: tuple[Day38UserManagementOutcome, ...]

    @property
    def any_management_succeeded(self) -> bool:
        return self.managed_count > 0 or self.already_applied_count > 0


class Day38MultiUserManagementService:
    """Apply one provider update independently to members that still own mapped exposure."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        management_service: Day38LiveUserManagementService,
    ) -> None:
        self._session_factory = session_factory
        self._management = management_service

    async def distribute(
        self,
        *,
        signal_id: UUID,
        lifecycle_event_id: UUID,
    ) -> Day38ManagementDistributionResult:
        targets = self._targets(signal_id)
        outcomes: list[Day38UserManagementOutcome] = []
        for user_id in targets:
            try:
                result = await self._management.execute_owner_demo_event(
                    owner_user_id=user_id,
                    lifecycle_event_id=lifecycle_event_id,
                )
            except Day27ManagementError as exc:
                outcome = Day38UserManagementOutcome(
                    user_id=user_id,
                    outcome="skipped",
                    broker_actions_sent=0,
                    positions_closed=0,
                    positions_modified=0,
                    orders_cancelled=0,
                    error_code=exc.code,
                )
                self._audit_user(signal_id, lifecycle_event_id, outcome)
                outcomes.append(outcome)
                continue
            except Exception:
                outcome = Day38UserManagementOutcome(
                    user_id=user_id,
                    outcome="skipped",
                    broker_actions_sent=0,
                    positions_closed=0,
                    positions_modified=0,
                    orders_cancelled=0,
                    error_code="day38_user_management_unavailable",
                )
                self._audit_user(signal_id, lifecycle_event_id, outcome)
                outcomes.append(outcome)
                continue

            outcome = Day38UserManagementOutcome(
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

        result = Day38ManagementDistributionResult(
            signal_id=signal_id,
            lifecycle_event_id=lifecycle_event_id,
            target_count=len(targets),
            managed_count=sum(item.outcome == "managed" for item in outcomes),
            skipped_count=sum(item.outcome == "skipped" for item in outcomes),
            already_applied_count=sum(item.outcome == "already_applied" for item in outcomes),
            outcomes=tuple(outcomes),
        )
        self._audit_summary(result)
        return result

    def _targets(self, signal_id: UUID) -> tuple[UUID, ...]:
        # Only users that actually hold mapped open/pending exposure for this Signal
        # receive lifecycle updates. A member that skipped the initial trade is never
        # pulled into later management for a trade they do not own.
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
        outcome: Day38UserManagementOutcome,
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

    def _audit_summary(self, result: Day38ManagementDistributionResult) -> None:
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
    "Day38ManagementDistributionResult",
    "Day38MultiUserManagementService",
    "Day38UserManagementOutcome",
]
