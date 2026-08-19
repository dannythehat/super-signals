from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.member_routing_canonical import (
    MemberDistributionService,
    MemberDistributionTarget,
    MemberManagementService,
)
from app.pending_reconciliation_canonical import AccountPendingReconciler
from app.trading_execution_canonical import (
    CanonicalTradingExecutionService,
    MemberTradingExecutionService,
)
from app.trading_management_canonical import (
    CanonicalTradingManagementService,
    MemberTradingManagementService,
)
from app.unified_pending_reconciler import LiveMemberPendingReconciler


def test_member_live_execution_inherits_exact_shared_engine() -> None:
    assert issubclass(MemberTradingExecutionService, CanonicalTradingExecutionService)
    assert "_size_signal" not in MemberTradingExecutionService.__dict__
    assert "_margin_preflight" not in MemberTradingExecutionService.__dict__
    assert "_create_layered_plans" not in MemberTradingExecutionService.__dict__
    assert "_validate_entry_timing" not in MemberTradingExecutionService.__dict__


def test_member_live_management_inherits_exact_shared_engine() -> None:
    assert issubclass(MemberTradingManagementService, CanonicalTradingManagementService)
    assert "_select_layer_positions" not in MemberTradingManagementService.__dict__
    assert "_needs_critical_management" not in MemberTradingManagementService.__dict__


def test_live_pending_reconciliation_inherits_exact_shared_algorithm() -> None:
    assert issubclass(LiveMemberPendingReconciler, AccountPendingReconciler)
    assert "reconcile_once" not in LiveMemberPendingReconciler.__dict__
    assert "_validate_fill" not in LiveMemberPendingReconciler.__dict__
    assert "_matching_history_order" not in LiveMemberPendingReconciler.__dict__
    assert "_persist_fill" not in LiveMemberPendingReconciler.__dict__
    assert "_persist_terminal_no_fill" not in LiveMemberPendingReconciler.__dict__


class _DistributionHarness(MemberDistributionService):
    def __init__(self, execution) -> None:
        self._execution = execution
        self._session_factory = None
        self.audits = []
        self.target = MemberDistributionTarget(
            user_id=uuid4(),
            risk_percent=Decimal("1"),
            allow_double_lot=True,
        )

    def _targets(self):
        return (self.target,)

    def _audit_user(self, **kwargs):
        self.audits.append(("user", kwargs))

    def _audit_summary(self, result):
        self.audits.append(("summary", result))


class _ExecutionProbe:
    def __init__(self) -> None:
        self.calls = []

    async def execute_live_user_signal(self, *, user_id, signal_id):
        self.calls.append((user_id, signal_id))
        return SimpleNamespace(
            base_risk_percent=Decimal("1"),
            positions=(
                SimpleNamespace(volume=Decimal("0.10")),
                SimpleNamespace(volume=Decimal("0.10")),
            ),
        )


@pytest.mark.asyncio
async def test_live_entry_switch_off_blocks_every_structure_without_execution(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", raising=False)
    execution = _ExecutionProbe()
    service = _DistributionHarness(execution)

    result = await service.distribute(signal_id=uuid4())

    assert result.executed_count == 0
    assert result.skipped_count == 1
    assert result.outcomes[0].error_code == "live_execution_disabled"
    assert execution.calls == []


@pytest.mark.asyncio
async def test_live_entry_switch_on_delegates_to_shared_engine_without_structure_filter(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", "true")
    execution = _ExecutionProbe()
    service = _DistributionHarness(execution)
    signal_id = uuid4()

    result = await service.distribute(signal_id=signal_id)

    assert result.executed_count == 1
    assert result.skipped_count == 0
    assert execution.calls == [(service.target.user_id, signal_id)]
    assert result.outcomes[0].position_count == 2
    assert result.outcomes[0].volume_per_position == (
        Decimal("0.10"),
        Decimal("0.10"),
    )


class _ManagementHarness(MemberManagementService):
    def __init__(self, management) -> None:
        self._management = management
        self._session_factory = None
        self.user_id = uuid4()
        self.audits = []

    def _targets(self, signal_id):
        return (self.user_id,)

    def _audit_user(self, *args, **kwargs):
        self.audits.append(("user", args, kwargs))

    def _audit_summary(self, result):
        self.audits.append(("summary", result))


class _ManagementProbe:
    def __init__(self) -> None:
        self.calls = []

    async def execute_owner_demo_event(self, *, owner_user_id, lifecycle_event_id):
        self.calls.append((owner_user_id, lifecycle_event_id))
        return SimpleNamespace(
            already_applied=False,
            broker_actions_sent=2,
            positions_closed=1,
            positions_modified=1,
            orders_cancelled=0,
        )


@pytest.mark.asyncio
async def test_live_management_switch_on_delegates_to_shared_engine_without_target_filter(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", "true")
    management = _ManagementProbe()
    service = _ManagementHarness(management)
    signal_id = uuid4()
    lifecycle_event_id = uuid4()

    result = await service.distribute(
        signal_id=signal_id,
        lifecycle_event_id=lifecycle_event_id,
    )

    assert result.managed_count == 1
    assert result.skipped_count == 0
    assert management.calls == [(service.user_id, lifecycle_event_id)]
    assert result.outcomes[0].broker_actions_sent == 2
