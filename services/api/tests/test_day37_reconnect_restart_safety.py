"""Day 37 reconnect/restart safety acceptance.

This is deliberately a proof layer over the accepted listener, execution and
Day 36 broker-authoritative reconciliation paths. No second retry engine exists.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.day28_full_execution import Day28FullExecutionRouter, _StoredDecision
from app.manual_reconciliation_day36 import (
    Day36ManualMt5ReconciliationService,
    _Account as Day36Account,
    _MappedPosition,
)
from app.mt5_connection_manager import Mt5ConnectionManager
from app.mt5_execution_day26 import Day26ExecutionError
from app.telegram_listener_day21 import Day21TelegramListenerManager
from app.telegram_listener_day28 import Day28TelegramListenerManager


class _Execution:
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
            double_lot_applied=False,
        )


class _Management:
    async def execute_owner_demo_event(self, **kwargs):  # pragma: no cover - never used here
        raise AssertionError(f"unexpected management call: {kwargs}")


class _Router(Day28FullExecutionRouter):
    def __init__(
        self,
        *,
        owner_id: UUID,
        source_id: UUID,
        execution: _Execution,
        signal_id: UUID,
    ) -> None:
        super().__init__(
            session_factory=lambda: None,
            owner_user_id=owner_id,
            execution_service=execution,
            management_service=_Management(),
            allowed_source_ids=(source_id,),
            risk_percent="1",
            double_lot_approved=False,
        )
        self._test_signal_id = signal_id
        self._execution_state = execution
        self.audits: list[dict] = []

    def _load_stored_decision(self, **kwargs):
        return _StoredDecision(
            message_id=uuid4(),
            decision="new_trade",
            action="execute",
            reason="v1_complete_signal",
        )

    def _resolve_signal_id(self, message_id, revision_index):
        return self._test_signal_id

    def _has_position_records(self, signal_id):
        return signal_id in self._execution_state.completed

    def _position_count(self, signal_id):
        return self._execution_state.completed.get(signal_id, 0)

    def _audit_success(self, *, entity_id, entity_type, payload):
        self.audits.append({"kind": "success", **payload})

    def _audit_failure(self, **kwargs):
        self.audits.append({"kind": "failure", **kwargs})


def test_broker_disconnect_blocks_live_signal_and_does_not_schedule_retry() -> None:
    owner, source, signal = uuid4(), uuid4(), uuid4()
    execution = _Execution()
    execution.failure_code = "mt5_account_not_connected"
    router = _Router(
        owner_id=owner,
        source_id=source,
        execution=execution,
        signal_id=signal,
    )

    result = asyncio.run(
        router.dispatch_stored_decision(
            source_id=source,
            telegram_message_id=37001,
        )
    )

    assert result.outcome == "blocked"
    assert result.error_code == "mt5_account_not_connected"
    assert len(execution.calls) == 1
    assert execution.completed == {}
    assert router.audits[-1]["kind"] == "failure"
    # Day 28 records a failure and returns. There is no reconnect retry queue or
    # delayed broker task which can turn this missed instruction into a later trade.
    assert router.audits[-1].get("automatic_retry") is not True


def test_fresh_router_after_service_restart_cannot_duplicate_existing_positions() -> None:
    owner, source, signal = uuid4(), uuid4(), uuid4()
    execution = _Execution()
    before_restart = _Router(
        owner_id=owner,
        source_id=source,
        execution=execution,
        signal_id=signal,
    )

    first = asyncio.run(
        before_restart.dispatch_stored_decision(
            source_id=source,
            telegram_message_id=37002,
        )
    )
    assert first.outcome == "executed"
    assert execution.completed[signal] == 3
    assert len(execution.calls) == 1

    # New router object = process-memory locks are gone. Only durable position
    # state is shared, exactly as after a Render restart.
    after_restart = _Router(
        owner_id=owner,
        source_id=source,
        execution=execution,
        signal_id=signal,
    )
    replay = asyncio.run(
        after_restart.dispatch_stored_decision(
            source_id=source,
            telegram_message_id=37002,
        )
    )

    assert replay.outcome == "already_applied"
    assert replay.already_applied is True
    assert replay.position_count == 3
    assert len(execution.calls) == 1


class _CatchupClient:
    def __init__(self, messages) -> None:
        self.messages = list(messages)

    async def get_messages(self, chat_id: int, limit: int):
        assert chat_id == -10037001
        assert limit == 25
        return self.messages


class _CatchupRouter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def dispatch_stored_decision(self, **kwargs):
        self.calls.append(dict(kwargs))
        raise AssertionError("historical catch-up must never dispatch to broker")


def test_restart_catchup_persists_missed_signal_as_evidence_but_never_executes(monkeypatch) -> None:
    source_id = uuid4()
    persisted: list[int] = []

    def persist_without_day28_dispatch(self, captured):
        persisted.append(captured.telegram_message_id)
        return True

    monkeypatch.setattr(
        Day21TelegramListenerManager,
        "_persist_message",
        persist_without_day28_dispatch,
    )
    manager = object.__new__(Day28TelegramListenerManager)
    manager._day28_router = _CatchupRouter()
    message = SimpleNamespace(
        id=37003,
        raw_text="XAUUSD BUY 4400 SL 4390 TP 4410",
        date=datetime(2026, 8, 13, 9, 0, tzinfo=UTC),
        edit_date=None,
        reply_to=None,
        media=None,
    )
    plan = SimpleNamespace(
        sources=(SimpleNamespace(source_id=source_id, chat_id=-10037001),)
    )

    asyncio.run(manager._catch_up_recent_messages(_CatchupClient([message]), plan))

    assert persisted == [37003]
    assert manager._day28_router.calls == []


class _ReconcileService:
    def __init__(self) -> None:
        self.calls = 0

    async def reconcile_all(self) -> int:
        self.calls += 1
        return 1


def test_mt5_connection_manager_reconciles_immediately_on_restart() -> None:
    async def scenario() -> int:
        service = _ReconcileService()
        manager = Mt5ConnectionManager(service, refresh_seconds=3600)  # type: ignore[arg-type]
        await manager.start()
        calls_after_start = service.calls
        await manager.stop()
        return calls_after_start

    assert asyncio.run(scenario()) == 1


USER = UUID("11111111-1111-4111-8111-111111111111")
SIGNAL = UUID("22222222-2222-4222-8222-222222222222")
POSITION = UUID("33333333-3333-4333-8333-333333333333")


class _Cipher:
    def decrypt(self, value: bytes) -> str:
        assert value == b"encrypted"
        return "token"


class _BrokerRead:
    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        return "london"

    async def read_positions(self, *, token: str, account_id: str, region: str):
        return [
            {
                "id": "broker-37",
                "stopLoss": 4397.5,
                "takeProfit": 4422.25,
                "updateTime": "2026-08-13T09:30:00Z",
            }
        ]


class _Day36RestartHarness(Day36ManualMt5ReconciliationService):
    def __init__(self, shared: dict[str, object]) -> None:
        self._session_factory = None
        self._cipher = _Cipher()
        self._gateway = _BrokerRead()
        self.shared = shared
        self.actions: list[tuple[str, str | None, str | None]] = []

    def _account(self, user_id: UUID):
        assert user_id == USER
        return Day36Account(account_id="account", token_ciphertext=b"encrypted")

    def _mapped_positions(self, user_id: UUID):
        return (
            _MappedPosition(
                id=POSITION,
                signal_id=SIGNAL,
                tp_index=1,
                broker_position_id="broker-37",
                status="open",
                stop_loss=self.shared["stop_loss"],
                take_profit=self.shared["take_profit"],
            ),
        )

    def _record_price_change(
        self,
        *,
        user_id,
        position,
        field,
        old_value,
        new_value,
        occurred_at,
    ):
        if self.shared[field] != old_value:
            return False
        self.shared[field] = new_value
        self.actions.append(
            (
                field,
                str(old_value) if old_value is not None else None,
                str(new_value) if new_value is not None else None,
            )
        )
        return True

    def _backfill_manual_closes(self, user_id: UUID) -> int:
        return 0

    def read_recent(self, user_id: UUID, *, limit: int = 12):
        return ()


def test_broker_sl_tp_survive_reconnect_and_fresh_service_restart() -> None:
    shared: dict[str, object] = {
        "stop_loss": Decimal("4390"),
        "take_profit": Decimal("4410"),
    }
    first_service = _Day36RestartHarness(shared)
    first = asyncio.run(first_service.reconcile(USER))

    assert first.stop_loss_changes == 1
    assert first.take_profit_changes == 1
    assert shared["stop_loss"] == Decimal("4397.5")
    assert shared["take_profit"] == Decimal("4422.25")
    assert first.broker_trade_action_created is False

    # New reconciliation-service instance represents a restart. Broker values are
    # still authoritative and therefore no stale local SL/TP is pushed back.
    restarted_service = _Day36RestartHarness(shared)
    second = asyncio.run(restarted_service.reconcile(USER))

    assert second.stop_loss_changes == 0
    assert second.take_profit_changes == 0
    assert shared["stop_loss"] == Decimal("4397.5")
    assert shared["take_profit"] == Decimal("4422.25")
    assert restarted_service.actions == []
    assert second.broker_trade_action_created is False
