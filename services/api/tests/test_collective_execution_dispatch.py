from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.collective_execution_dispatch import CollectiveAwareCanonicalExecutionDispatcher
from app.execution_dispatch_canonical import StoredDecision


class _FakeExecution:
    async def execute_owner_demo_signal(self, **kwargs):  # pragma: no cover - not used here
        raise AssertionError("new trade execution must not be used by management fanout")


class _FakeManagement:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def execute_owner_demo_event(self, *, owner_user_id, lifecycle_event_id):
        del owner_user_id
        self.calls.append(lifecycle_event_id)
        return SimpleNamespace(
            actions_requested=1,
            broker_actions_sent=1,
            positions_closed=0,
            positions_modified=1,
            orders_cancelled=0,
            already_applied=False,
        )


class _NoMembers:
    async def distribute(self, **kwargs):
        del kwargs
        return SimpleNamespace(
            target_count=0,
            executed_count=0,
            skipped_count=0,
            managed_count=0,
            already_applied_count=0,
            any_management_succeeded=False,
            outcomes=(),
        )


class _FanoutHarness(CollectiveAwareCanonicalExecutionDispatcher):
    def __init__(self) -> None:
        self.management = _FakeManagement()
        super().__init__(
            session_factory=lambda: None,
            owner_user_id=uuid4(),
            execution_service=_FakeExecution(),
            management_service=self.management,
            member_distribution=_NoMembers(),  # type: ignore[arg-type]
            member_management=_NoMembers(),  # type: ignore[arg-type]
            risk_percent="1",
            double_lot_approved=True,
        )
        self.message_id = uuid4()
        self.event_a = uuid4()
        self.event_b = uuid4()
        self.signal_a = uuid4()
        self.signal_b = uuid4()
        self.stored = StoredDecision(
            message_id=self.message_id,
            decision="trade_update",
            action="apply_update",
            reason="day27_explicit_management",
        )
        self.audits: list[tuple[str, dict]] = []

    def _load_stored_decision(self, **kwargs):
        del kwargs
        return self.stored

    def _resolve_lifecycle_events(self, message_id, revision_index):
        assert message_id == self.message_id
        assert revision_index == 0
        return (
            (self.event_a, self.signal_a, True),
            (self.event_b, self.signal_b, True),
        )

    def _active_exposure_count(self, signal_id):
        assert signal_id in {self.signal_a, self.signal_b}
        return 1

    def _audit_success(self, *, entity_id, entity_type, payload):
        self.audits.append(("success", dict(payload)))

    def _audit_failure(self, **kwargs):
        self.audits.append(("failure", dict(kwargs)))



def _fxt_extracted() -> dict:
    return {
        "symbol": None,
        "update_type": "edit_stop_loss",
        "update_target": "all",
        "update_value": "4368",
        "management_actions": [
            {"type": "edit_stop_loss", "target": "all", "value": "4368"}
        ],
    }


def test_fxt_all_gold_stoplosses_is_explicit_collective_management() -> None:
    raw = (
        "MOVE ALL YOUR GOLD STOPLOSSES TO 4368\n\n"
        "IT WANTS TO GO FOR DOWNSIDE LIQUIDITY.\n\n"
        "WE WILL OPEN NEW BUYS FOR THE JACKPOT SOON."
    )
    assert (
        CollectiveAwareCanonicalExecutionDispatcher._is_explicit_collective_management(
            raw,
            _fxt_extracted(),
        )
        is True
    )


def test_bare_sl_update_stays_fail_closed_not_provider_wide() -> None:
    raw = "Move your gold SL to 4368."
    assert (
        CollectiveAwareCanonicalExecutionDispatcher._is_explicit_collective_management(
            raw,
            _fxt_extracted(),
        )
        is False
    )


def test_all_without_named_instrument_stays_fail_closed() -> None:
    raw = "Move all stoplosses to 4368."
    assert (
        CollectiveAwareCanonicalExecutionDispatcher._is_explicit_collective_management(
            raw,
            _fxt_extracted(),
        )
        is False
    )


@pytest.mark.asyncio
async def test_collective_management_routes_every_linked_signal_once() -> None:
    router = _FanoutHarness()

    result = await router.dispatch_stored_decision(
        source_id=uuid4(),
        telegram_message_id=84737,
        revision_index=0,
    )

    assert result.outcome == "managed"
    assert result.reason == "collective_management_applied"
    assert result.broker_actions_sent == 2
    assert router.management.calls == [router.event_a, router.event_b]
    assert [kind for kind, _payload in router.audits] == ["success", "success"]
