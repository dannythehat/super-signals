from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.day28_full_execution import Day28FullExecutionRouter, _StoredDecision
from app.mt5_execution_day26 import Day26ExecutionError


class FakeExecutionService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.completed: dict[UUID, int] = {}
        self.failure_code: str | None = None

    async def execute_owner_demo_signal(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.failure_code:
            raise Day26ExecutionError(self.failure_code)
        signal_id = kwargs["signal_id"]
        self.completed[signal_id] = 3
        return SimpleNamespace(
            positions=(object(), object(), object()),
            double_lot_applied=True,
        )


class FakeManagementService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.applied: set[UUID] = set()

    async def execute_owner_demo_event(self, **kwargs):
        self.calls.append(dict(kwargs))
        event_id = kwargs["lifecycle_event_id"]
        already = event_id in self.applied
        self.applied.add(event_id)
        return SimpleNamespace(
            actions_requested=2,
            broker_actions_sent=0 if already else 2,
            positions_closed=0,
            positions_modified=0 if already else 2,
            orders_cancelled=0,
            already_applied=already,
        )


class RouterHarness(Day28FullExecutionRouter):
    def __init__(
        self,
        *,
        owner_user_id: UUID,
        source_id: UUID,
        execution: FakeExecutionService,
        management: FakeManagementService,
    ) -> None:
        super().__init__(
            session_factory=lambda: None,  # overridden DB helpers below
            owner_user_id=owner_user_id,
            execution_service=execution,
            management_service=management,
            allowed_source_ids=(source_id,),
            risk_percent="1",
            double_lot_approved=True,
        )
        self.execution = execution
        self.management = management
        self.stored: _StoredDecision | None = None
        self.signal_id = uuid4()
        self.lifecycle_event_id = uuid4()
        self.audits: list[tuple[str, dict]] = []
        self.load_calls = 0

    def _load_stored_decision(self, **kwargs):
        self.load_calls += 1
        return self.stored

    def _resolve_signal_id(self, message_id, revision_index):
        return self.signal_id

    def _resolve_lifecycle_event(self, message_id, revision_index):
        return self.lifecycle_event_id, self.signal_id

    def _has_position_records(self, signal_id):
        return signal_id in self.execution.completed

    def _position_count(self, signal_id):
        return self.execution.completed.get(signal_id, 0)

    def _audit_success(self, *, entity_id, entity_type, payload):
        self.audits.append(("success", dict(payload)))

    def _audit_failure(self, **kwargs):
        self.audits.append(("failure", dict(kwargs)))


def decision(*, kind: str, action: str, reason: str = "test") -> _StoredDecision:
    return _StoredDecision(
        message_id=uuid4(),
        decision=kind,
        action=action,
        reason=reason,
    )


@pytest.mark.asyncio
async def test_new_trade_uses_one_percent_and_provider_double_lot_then_dedups() -> None:
    owner = uuid4()
    source = uuid4()
    execution = FakeExecutionService()
    management = FakeManagementService()
    router = RouterHarness(
        owner_user_id=owner,
        source_id=source,
        execution=execution,
        management=management,
    )
    router.stored = decision(kind="new_trade", action="execute")

    first = await router.dispatch_stored_decision(
        source_id=source,
        telegram_message_id=101,
    )
    second = await router.dispatch_stored_decision(
        source_id=source,
        telegram_message_id=101,
    )

    assert first.outcome == "executed"
    assert first.position_count == 3
    assert len(execution.calls) == 1
    assert execution.calls[0]["owner_user_id"] == owner
    assert execution.calls[0]["risk_percent"] == "1"
    assert execution.calls[0]["double_lot_approved"] is True

    assert second.outcome == "already_applied"
    assert second.already_applied is True
    assert len(execution.calls) == 1


@pytest.mark.asyncio
async def test_chatter_never_reaches_either_broker_service() -> None:
    source = uuid4()
    execution = FakeExecutionService()
    management = FakeManagementService()
    router = RouterHarness(
        owner_user_id=uuid4(),
        source_id=source,
        execution=execution,
        management=management,
    )
    router.stored = decision(kind="chatter", action="ignore", reason="provider_chatter")

    result = await router.dispatch_stored_decision(
        source_id=source,
        telegram_message_id=202,
    )

    assert result.outcome == "ignored"
    assert result.reason == "provider_chatter"
    assert execution.calls == []
    assert management.calls == []


@pytest.mark.asyncio
async def test_linked_management_routes_to_day27_and_replay_is_idempotent() -> None:
    source = uuid4()
    execution = FakeExecutionService()
    management = FakeManagementService()
    router = RouterHarness(
        owner_user_id=uuid4(),
        source_id=source,
        execution=execution,
        management=management,
    )
    router.stored = decision(kind="trade_update", action="apply_update")

    first = await router.dispatch_stored_decision(
        source_id=source,
        telegram_message_id=303,
    )
    second = await router.dispatch_stored_decision(
        source_id=source,
        telegram_message_id=303,
    )

    assert first.outcome == "managed"
    assert first.broker_actions_sent == 2
    assert second.outcome == "already_applied"
    assert second.already_applied is True
    assert len(management.calls) == 2
    assert management.calls[0]["lifecycle_event_id"] == router.lifecycle_event_id


@pytest.mark.asyncio
async def test_day26_failure_code_is_returned_once_without_retry() -> None:
    source = uuid4()
    execution = FakeExecutionService()
    execution.failure_code = "entry_price_unavailable"
    management = FakeManagementService()
    router = RouterHarness(
        owner_user_id=uuid4(),
        source_id=source,
        execution=execution,
        management=management,
    )
    router.stored = decision(kind="new_trade", action="execute")

    result = await router.dispatch_stored_decision(
        source_id=source,
        telegram_message_id=404,
    )

    assert result.outcome == "blocked"
    assert result.error_code == "entry_price_unavailable"
    assert len(execution.calls) == 1
    assert router.audits[-1][0] == "failure"


@pytest.mark.asyncio
async def test_source_outside_day28_allowlist_never_loads_or_trades() -> None:
    allowed = uuid4()
    execution = FakeExecutionService()
    management = FakeManagementService()
    router = RouterHarness(
        owner_user_id=uuid4(),
        source_id=allowed,
        execution=execution,
        management=management,
    )
    router.stored = decision(kind="new_trade", action="execute")

    result = await router.dispatch_stored_decision(
        source_id=uuid4(),
        telegram_message_id=505,
    )

    assert result.outcome == "ignored"
    assert result.reason == "source_not_in_day28_allowlist"
    assert router.load_calls == 0
    assert execution.calls == []
    assert management.calls == []
