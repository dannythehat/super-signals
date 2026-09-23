"""Bounded retry for retryable management errors, and probation excluding member LIVE fan-out.

Root cause this guards against: a management instruction ("close"/"move to breakeven"/etc)
that failed once used to be gone for good -- ``CanonicalExecutionDispatcher`` recorded one
failure audit event and never tried again, leaving the position open and unprotected. This
covers the two additive fixes: (1) a gateway-marked-retryable failure gets retried a bounded
number of times before giving up, and a non-retryable (genuinely ambiguous) failure is never
retried here; (2) a still-probationary provider's new trade never reaches a member's real
LIVE account, even while the global LIVE switch is on, matching the owner's own plan that
newly switched-on providers are paper traded until graduated.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.active_account_member_routing import ActiveAccountMemberDistributionService
from app.mt5_management_day27 import Day27ManagementError
from tests.test_canonical_execution_dispatch import (
    FakeExecutionService,
    RouterHarness,
    decision,
)


class _RetryableThenSucceeds:
    def __init__(self, *, fail_times: int, retryable: bool) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.retryable = retryable

    async def execute_owner_demo_event(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise Day27ManagementError("mt5_account_not_connected", retryable=self.retryable)
        return SimpleNamespace(
            actions_requested=1,
            broker_actions_sent=1,
            positions_closed=0,
            positions_modified=1,
            orders_cancelled=0,
            already_applied=False,
        )


@pytest.mark.asyncio
async def test_retryable_management_error_succeeds_on_a_later_attempt(monkeypatch) -> None:
    import app.execution_dispatch_canonical as mod

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    execution = FakeExecutionService()
    management = _RetryableThenSucceeds(fail_times=1, retryable=True)
    router = RouterHarness(owner_user_id=uuid4(), execution=execution, management=management)
    execution.completed[router.signal_id] = 3
    router.stored = decision(kind="trade_update", action="apply_update")

    result = await router.dispatch_stored_decision(source_id=uuid4(), telegram_message_id=606)

    assert result.outcome == "managed"
    assert management.calls == 2


@pytest.mark.asyncio
async def test_non_retryable_management_error_is_never_retried(monkeypatch) -> None:
    import app.execution_dispatch_canonical as mod

    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("must not sleep/retry a non-retryable error")

    monkeypatch.setattr(mod.asyncio, "sleep", _fail_if_called)

    execution = FakeExecutionService()
    management = _RetryableThenSucceeds(fail_times=99, retryable=False)
    router = RouterHarness(owner_user_id=uuid4(), execution=execution, management=management)
    execution.completed[router.signal_id] = 3
    router.stored = decision(kind="trade_update", action="apply_update")

    result = await router.dispatch_stored_decision(source_id=uuid4(), telegram_message_id=707)

    assert result.outcome == "blocked"
    assert management.calls == 1


@pytest.mark.asyncio
async def test_retry_gives_up_after_its_bounded_attempt_budget(monkeypatch) -> None:
    import app.execution_dispatch_canonical as mod

    sleeps = []

    async def _count_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(mod.asyncio, "sleep", _count_sleep)

    execution = FakeExecutionService()
    management = _RetryableThenSucceeds(fail_times=99, retryable=True)
    router = RouterHarness(owner_user_id=uuid4(), execution=execution, management=management)
    execution.completed[router.signal_id] = 3
    router.stored = decision(kind="trade_update", action="apply_update")

    result = await router.dispatch_stored_decision(source_id=uuid4(), telegram_message_id=808)

    assert result.outcome == "blocked"
    assert management.calls == mod._MANAGEMENT_RETRY_ATTEMPTS
    assert len(sleeps) == mod._MANAGEMENT_RETRY_ATTEMPTS - 1


class _DemoExecution:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def execute_owner_demo_signal(self, *, owner_user_id, **_kwargs):
        self.calls.append(owner_user_id)
        return SimpleNamespace(
            positions=(SimpleNamespace(volume=1),), double_lot_applied=False
        )


class _LiveExecution:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def execute_live_user_signal(self, *, user_id, **_kwargs):
        self.calls.append(user_id)
        return SimpleNamespace(
            positions=(SimpleNamespace(volume=1),), double_lot_applied=False
        )


class _Harness(ActiveAccountMemberDistributionService):
    def __init__(self, *, targets) -> None:
        self.demo = _DemoExecution()
        self.live = _LiveExecution()
        super().__init__(
            session_factory=lambda: None,  # type: ignore[arg-type]
            demo_execution_service=self.demo,
            live_execution_service=self.live,
        )
        self._fixed_targets = targets

    def _targets(self):
        return self._fixed_targets

    def _audit_user(self, **_kwargs):
        return None

    def _audit_summary(self, *_args, **_kwargs):
        return None


def _target(user_id, environment):
    from decimal import Decimal

    return SimpleNamespace(
        user_id=user_id, risk_percent=Decimal("1"), allow_double_lot=False, account_environment=environment
    )


@pytest.mark.asyncio
async def test_probation_exclude_live_skips_only_live_members_not_demo(monkeypatch) -> None:
    import app.active_account_member_routing as mod

    monkeypatch.setattr(mod, "live_execution_enabled", lambda: True)

    demo_user = uuid4()
    live_user = uuid4()
    service = _Harness(targets=[_target(demo_user, "demo"), _target(live_user, "live")])

    result = await service.distribute(signal_id=uuid4(), exclude_live=True)

    assert service.demo.calls == [demo_user]
    assert service.live.calls == []
    live_outcome = next(o for o in result.outcomes if o.user_id == live_user)
    assert live_outcome.outcome == "skipped"
    assert live_outcome.error_code == "probation_live_execution_excluded"


@pytest.mark.asyncio
async def test_not_on_probation_live_still_dispatches_when_switch_is_on(monkeypatch) -> None:
    import app.active_account_member_routing as mod

    monkeypatch.setattr(mod, "live_execution_enabled", lambda: True)

    live_user = uuid4()
    service = _Harness(targets=[_target(live_user, "live")])

    result = await service.distribute(signal_id=uuid4(), exclude_live=False)

    assert service.live.calls == [live_user]
    assert result.executed_count == 1



def test_management_reliability_covers_never_attempted_provider_updates() -> None:
    from pathlib import Path

    source = Path("services/api/app/management_reliability_runtime.py").read_text()
    assert "_UNATTEMPTED_SQL" in source
    assert "mt5.day28_route_success','mt5.day28_route_failure" in source
    assert "jsonb_array_length" in source
    assert "broker_position_id IS NOT NULL" in source
    assert "broker_order_id IS NOT NULL" in source
    assert "_DEFAULT_INTERVAL_SECONDS = 30" in source
