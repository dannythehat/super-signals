"""Ordinary-member distribution by each user's explicitly active MT5 environment.

Demo accounts always participate in Smart Signals paper trading. Real accounts participate
in new-entry mutation only while the single global LIVE entry switch is enabled. Provider
management of already-open exposure is never disabled by that entry switch: protection and
close instructions must remain available for positions that already exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.member_routing_canonical import (
    MemberDistributionOutcome,
    MemberDistributionResult,
    MemberDistributionService,
    MemberManagementOutcome,
    MemberManagementResult,
    MemberManagementService,
    live_execution_enabled,
)
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_management_day27 import Day27ManagementError


@dataclass(frozen=True, slots=True)
class ActiveMemberTarget:
    user_id: UUID
    risk_percent: Decimal
    allow_double_lot: bool
    account_environment: str


@dataclass(frozen=True, slots=True)
class ActiveManagementTarget:
    user_id: UUID
    account_environment: str


class ActiveAccountMemberDistributionService(MemberDistributionService):
    """Fan a signal to Demo or Real according to each member's active account."""

    def __init__(self, *, session_factory, demo_execution_service, live_execution_service) -> None:
        super().__init__(session_factory=session_factory, execution_service=live_execution_service)
        self._demo_execution = demo_execution_service
        self._live_execution = live_execution_service

    async def distribute(
        self, *, signal_id: UUID, exclude_live: bool = False
    ) -> MemberDistributionResult:
        targets = self._targets()
        outcomes: list[MemberDistributionOutcome] = []
        for target in targets:
            try:
                if target.account_environment == "demo":
                    execution = await self._demo_execution.execute_owner_demo_signal(
                        owner_user_id=target.user_id,
                        signal_id=signal_id,
                        risk_percent=str(target.risk_percent),
                        double_lot_approved=target.allow_double_lot,
                    )
                elif target.account_environment == "live":
                    if exclude_live:
                        outcome = MemberDistributionOutcome(
                            user_id=target.user_id,
                            outcome="skipped",
                            risk_percent=target.risk_percent,
                            allow_double_lot=target.allow_double_lot,
                            position_count=0,
                            volume_per_position=(),
                            error_code="probation_live_execution_excluded",
                        )
                        self._audit_user(signal_id=signal_id, outcome=outcome)
                        outcomes.append(outcome)
                        continue
                    if not live_execution_enabled():
                        outcome = MemberDistributionOutcome(
                            user_id=target.user_id,
                            outcome="skipped",
                            risk_percent=target.risk_percent,
                            allow_double_lot=target.allow_double_lot,
                            position_count=0,
                            volume_per_position=(),
                            error_code="live_execution_disabled",
                        )
                        self._audit_user(signal_id=signal_id, outcome=outcome)
                        outcomes.append(outcome)
                        continue
                    execution = await self._live_execution.execute_live_user_signal(
                        user_id=target.user_id,
                        signal_id=signal_id,
                    )
                else:
                    raise Day26ExecutionError("mt5_account_environment_invalid")
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
                    error_code="member_execution_unavailable",
                )
            else:
                outcome = MemberDistributionOutcome(
                    user_id=target.user_id,
                    outcome="executed",
                    risk_percent=target.risk_percent,
                    allow_double_lot=target.allow_double_lot,
                    position_count=len(execution.positions),
                    volume_per_position=tuple(item.volume for item in execution.positions),
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

    def _targets(self) -> tuple[ActiveMemberTarget, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT
                        u.id,
                        utc.risk_percent,
                        utc.allow_double_lot,
                        lower(m.account_environment) AS account_environment
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN user_trading_controls AS utc ON utc.user_id=u.id
                    JOIN mt5_accounts AS m
                      ON m.owner_user_id=u.id
                     AND m.status='connected'
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
            ActiveMemberTarget(
                user_id=UUID(str(row["id"])),
                risk_percent=Decimal(str(row["risk_percent"])),
                allow_double_lot=bool(row["allow_double_lot"]),
                account_environment=str(row["account_environment"] or ""),
            )
            for row in rows
        )


class ActiveAccountMemberManagementService(MemberManagementService):
    """Keep provider protection attached to the account that owns existing exposure."""

    def __init__(self, *, session_factory, demo_management_service, live_management_service) -> None:
        super().__init__(session_factory=session_factory, management_service=live_management_service)
        self._demo_management = demo_management_service
        self._live_management = live_management_service

    async def distribute(
        self,
        *,
        signal_id: UUID,
        lifecycle_event_id: UUID,
    ) -> MemberManagementResult:
        targets = self._targets(signal_id)
        outcomes: list[MemberManagementOutcome] = []
        for target in targets:
            try:
                service = (
                    self._demo_management
                    if target.account_environment == "demo"
                    else self._live_management
                    if target.account_environment == "live"
                    else None
                )
                if service is None:
                    raise Day27ManagementError("mt5_account_environment_invalid")
                result = await service.execute_owner_demo_event(
                    owner_user_id=target.user_id,
                    lifecycle_event_id=lifecycle_event_id,
                )
            except Day27ManagementError as exc:
                outcome = MemberManagementOutcome(
                    user_id=target.user_id,
                    outcome="skipped",
                    broker_actions_sent=0,
                    positions_closed=0,
                    positions_modified=0,
                    orders_cancelled=0,
                    error_code=exc.code,
                )
            except Exception:
                outcome = MemberManagementOutcome(
                    user_id=target.user_id,
                    outcome="skipped",
                    broker_actions_sent=0,
                    positions_closed=0,
                    positions_modified=0,
                    orders_cancelled=0,
                    error_code="member_management_unavailable",
                )
            else:
                outcome = MemberManagementOutcome(
                    user_id=target.user_id,
                    outcome="already_applied" if result.already_applied else "managed",
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
            already_applied_count=sum(item.outcome == "already_applied" for item in outcomes),
            outcomes=tuple(outcomes),
        )
        self._audit_summary(result)
        return result

    def _targets(self, signal_id: UUID) -> tuple[ActiveManagementTarget, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT p.user_id, lower(m.account_environment) AS account_environment
                    FROM positions AS p
                    JOIN users AS u ON u.id=p.user_id AND u.status='active'
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN mt5_accounts AS m
                      ON m.owner_user_id=p.user_id
                     AND m.status='connected'
                    WHERE p.signal_id=:signal_id
                      AND p.status IN ('open','pending')
                    ORDER BY p.user_id
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().all()
        return tuple(
            ActiveManagementTarget(
                user_id=UUID(str(row["user_id"])),
                account_environment=str(row["account_environment"] or ""),
            )
            for row in rows
        )


__all__ = [
    "ActiveAccountMemberDistributionService",
    "ActiveAccountMemberManagementService",
]
