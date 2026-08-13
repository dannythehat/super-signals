"""Day 38 independent fan-out of one canonical Signal to active invited users.

Each user is an isolated execution attempt. A broker/risk/funds failure for one user
is recorded for that user and never aborts another user's attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_execution_day38 import Day38LiveUserExecutionService


@dataclass(frozen=True, slots=True)
class Day38DistributionTarget:
    user_id: UUID
    risk_percent: Decimal
    allow_double_lot: bool


@dataclass(frozen=True, slots=True)
class Day38UserDistributionOutcome:
    user_id: UUID
    outcome: str
    risk_percent: Decimal
    allow_double_lot: bool
    position_count: int
    volume_per_position: tuple[Decimal, ...]
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class Day38DistributionResult:
    signal_id: UUID
    target_count: int
    executed_count: int
    skipped_count: int
    outcomes: tuple[Day38UserDistributionOutcome, ...]

    @property
    def any_execution_succeeded(self) -> bool:
        return self.executed_count > 0


class Day38MultiUserDistributionService:
    """Fan out independently; never let one user's failure stop another user."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        execution_service: Day38LiveUserExecutionService,
    ) -> None:
        self._session_factory = session_factory
        self._execution = execution_service

    async def distribute(self, *, signal_id: UUID) -> Day38DistributionResult:
        targets = self._targets()
        outcomes: list[Day38UserDistributionOutcome] = []

        for target in targets:
            try:
                execution = await self._execution.execute_live_user_signal(
                    user_id=target.user_id,
                    signal_id=signal_id,
                )
            except Day26ExecutionError as exc:
                outcome = Day38UserDistributionOutcome(
                    user_id=target.user_id,
                    outcome="skipped",
                    risk_percent=target.risk_percent,
                    allow_double_lot=target.allow_double_lot,
                    position_count=0,
                    volume_per_position=(),
                    error_code=exc.code,
                )
                self._audit_user(signal_id=signal_id, outcome=outcome)
                outcomes.append(outcome)
                continue
            except Exception:
                # User isolation is the Day 38 contract. An unexpected single-account
                # fault is contained and recorded without exposing a credential/error.
                outcome = Day38UserDistributionOutcome(
                    user_id=target.user_id,
                    outcome="skipped",
                    risk_percent=target.risk_percent,
                    allow_double_lot=target.allow_double_lot,
                    position_count=0,
                    volume_per_position=(),
                    error_code="day38_user_execution_unavailable",
                )
                self._audit_user(signal_id=signal_id, outcome=outcome)
                outcomes.append(outcome)
                continue

            outcome = Day38UserDistributionOutcome(
                user_id=target.user_id,
                outcome="executed",
                risk_percent=execution.base_risk_percent,
                allow_double_lot=target.allow_double_lot,
                position_count=len(execution.positions),
                volume_per_position=tuple(position.volume for position in execution.positions),
                error_code=None,
            )
            self._audit_user(signal_id=signal_id, outcome=outcome)
            outcomes.append(outcome)

        result = Day38DistributionResult(
            signal_id=signal_id,
            target_count=len(targets),
            executed_count=sum(item.outcome == "executed" for item in outcomes),
            skipped_count=sum(item.outcome == "skipped" for item in outcomes),
            outcomes=tuple(outcomes),
        )
        self._audit_summary(result)
        return result

    def _targets(self) -> tuple[Day38DistributionTarget, ...]:
        # Target selection is intentionally broader than "currently connected".
        # Every active ordinary user with automation ON gets an independent attempt;
        # the live execution boundary then fails closed for approval/connectivity/etc.
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT u.id, utc.risk_percent, utc.allow_double_lot
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN user_trading_controls AS utc ON utc.user_id=u.id
                    WHERE u.status='active'
                      AND utc.trading_status='active'
                    ORDER BY u.id
                    """
                )
            ).mappings().all()
        return tuple(
            Day38DistributionTarget(
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
        outcome: Day38UserDistributionOutcome,
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

    def _audit_summary(self, result: Day38DistributionResult) -> None:
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


__all__ = [
    "Day38DistributionResult",
    "Day38DistributionTarget",
    "Day38MultiUserDistributionService",
    "Day38UserDistributionOutcome",
]
