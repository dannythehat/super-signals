from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.mt5_execution_day38 import Day38LiveUserExecutionService
from app.mt5_management_day38 import Day38LiveUserManagementService
from app.multi_user_distribution_day38 import (
    Day38DistributionResult,
    Day38MultiUserDistributionService,
)
from app.multi_user_management_day38 import (
    Day38ManagementDistributionResult,
    Day38MultiUserManagementService,
)
from app.paper_critical_management_v2 import PaperCriticalManagementV2
from app.paper_fresh_start_execution import PaperFreshStartExecutionService
from app.paper_pending_reconciler import PaperPendingReconciler
from app.paper_safe_member_routing import (
    PaperSafeMemberDistribution,
    PaperSafeMemberManagement,
)
from app.unified_pending_reconciler import LiveMemberPendingReconciler


def test_live_execution_inherits_exact_paper_tested_engine() -> None:
    assert issubclass(Day38LiveUserExecutionService, PaperFreshStartExecutionService)
    # Trading policy must remain inherited, not reimplemented for LIVE.
    assert "_size_signal" not in Day38LiveUserExecutionService.__dict__
    assert "_margin_preflight" not in Day38LiveUserExecutionService.__dict__
    assert "_create_layered_plans" not in Day38LiveUserExecutionService.__dict__
    assert "_validate_entry_timing" not in Day38LiveUserExecutionService.__dict__


def test_live_management_inherits_exact_paper_tested_engine() -> None:
    assert issubclass(Day38LiveUserManagementService, PaperCriticalManagementV2)
    # Layer/partial/best-entry/provider-price behaviour must remain shared.
    assert "_select_layer_positions" not in Day38LiveUserManagementService.__dict__
    assert "_needs_critical_management" not in Day38LiveUserManagementService.__dict__


def test_live_pending_fill_reconciliation_reuses_paper_algorithm() -> None:
    assert issubclass(LiveMemberPendingReconciler, PaperPendingReconciler)
    assert "_validate_fill" not in LiveMemberPendingReconciler.__dict__
    assert "_persist_fill" not in LiveMemberPendingReconciler.__dict__


@pytest.mark.asyncio
async def test_live_entry_switch_off_blocks_every_structure_without_execution(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", raising=False)
    target = SimpleNamespace(user_id=uuid4(), risk_percent=1, allow_double_lot=True)
    service = object.__new__(PaperSafeMemberDistribution)
    service._targets = lambda: (target,)
    service._audit_user = lambda **kwargs: None
    service._audit_summary = lambda result: None

    result = await service.distribute(signal_id=uuid4())

    assert result.executed_count == 0
    assert result.skipped_count == 1
    assert result.outcomes[0].error_code == "live_execution_disabled"


@pytest.mark.asyncio
async def test_live_entry_switch_on_delegates_without_structure_filter(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", "true")
    expected = Day38DistributionResult(
        signal_id=uuid4(),
        target_count=0,
        executed_count=0,
        skipped_count=0,
        outcomes=(),
    )
    calls = []

    async def delegated(self, *, signal_id):
        calls.append(signal_id)
        return expected

    monkeypatch.setattr(Day38MultiUserDistributionService, "distribute", delegated)
    service = object.__new__(PaperSafeMemberDistribution)
    signal_id = uuid4()

    result = await service.distribute(signal_id=signal_id)

    assert result is expected
    assert calls == [signal_id]


@pytest.mark.asyncio
async def test_live_management_switch_on_delegates_without_target_filter(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", "true")
    expected = Day38ManagementDistributionResult(
        signal_id=uuid4(),
        lifecycle_event_id=uuid4(),
        target_count=0,
        managed_count=0,
        skipped_count=0,
        already_applied_count=0,
        outcomes=(),
    )
    calls = []

    async def delegated(self, *, signal_id, lifecycle_event_id):
        calls.append((signal_id, lifecycle_event_id))
        return expected

    monkeypatch.setattr(Day38MultiUserManagementService, "distribute", delegated)
    service = object.__new__(PaperSafeMemberManagement)
    signal_id = uuid4()
    lifecycle_event_id = uuid4()

    result = await service.distribute(
        signal_id=signal_id,
        lifecycle_event_id=lifecycle_event_id,
    )

    assert result is expected
    assert calls == [(signal_id, lifecycle_event_id)]
