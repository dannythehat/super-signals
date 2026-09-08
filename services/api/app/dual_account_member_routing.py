"""Member distribution aware of the selected Paper/Real account environment."""

from __future__ import annotations

from app.dual_account_execution import active_environment
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


class DualAccountMemberDistributionService(MemberDistributionService):
    """Paper broker mutation is allowed; Real still requires the global live switch."""

    async def distribute(self, *, signal_id):  # noqa: ANN001
        targets = self._targets()
        live_enabled = live_execution_enabled()
        outcomes: list[MemberDistributionOutcome] = []

        for target in targets:
            environment = active_environment(self._session_factory, target.user_id)
            if environment not in {"demo", "live"}:
                outcome = MemberDistributionOutcome(
                    user_id=target.user_id,
                    outcome="skipped",
                    risk_percent=target.risk_percent,
                    allow_double_lot=target.allow_double_lot,
                    position_count=0,
                    volume_per_position=(),
                    error_code="mt5_active_account_not_selected",
                )
            elif environment == "live" and not live_enabled:
                outcome = MemberDistributionOutcome(
                    user_id=target.user_id,
                    outcome="skipped",
                    risk_percent=target.risk_percent,
                    allow_double_lot=target.allow_double_lot,
                    position_count=0,
                    volume_per_position=(),
                    error_code="live_execution_disabled",
                )
            else:
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


class DualAccountMemberManagementService(MemberManagementService):
    """Continue Paper management without enabling ordinary-member Real mutation."""

    async def distribute(self, *, signal_id, lifecycle_event_id):  # noqa: ANN001
        targets = self._targets(signal_id)
        live_enabled = live_execution_enabled()
        outcomes: list[MemberManagementOutcome] = []

        for user_id in targets:
            environment = active_environment(self._session_factory, user_id)
            if environment not in {"demo", "live"}:
                outcome = MemberManagementOutcome(
                    user_id=user_id,
                    outcome="skipped",
                    broker_actions_sent=0,
                    positions_closed=0,
                    positions_modified=0,
                    orders_cancelled=0,
                    error_code="mt5_active_account_not_selected",
                )
            elif environment == "live" and not live_enabled:
                outcome = MemberManagementOutcome(
                    user_id=user_id,
                    outcome="skipped",
                    broker_actions_sent=0,
                    positions_closed=0,
                    positions_modified=0,
                    orders_cancelled=0,
                    error_code="live_execution_disabled",
                )
            else:
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


__all__ = [
    "DualAccountMemberDistributionService",
    "DualAccountMemberManagementService",
]
