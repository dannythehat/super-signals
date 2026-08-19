"""Canonical reconnect/restart safety acceptance.

This proves stale-entry recovery safety, immediate MT5 reconciliation, and
broker-authoritative SL/TP truth across fresh service instances. Durable route
idempotency and no-automatic-retry behavior are covered by test_canonical_execution_dispatch.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.manual_reconciliation_day36 import (
    Day36ManualMt5ReconciliationService,
    _Account as Day36Account,
    _MappedPosition,
)
from app.mt5_connection_manager import Mt5ConnectionManager
from app.telegram_listener_canonical import CanonicalProductionTelegramListenerManager


class _CatchupClient:
    def __init__(self, messages) -> None:
        self.messages = list(messages)

    async def get_messages(self, chat_id: int, limit: int):
        assert chat_id == -10037001
        assert limit == 50
        return self.messages


class _StoredStaleEntryRouter:
    def _load_stored_decision(self, **kwargs):
        return SimpleNamespace(
            message_id=uuid4(),
            decision="new_trade",
            action="execute",
        )


def test_restart_recovery_persists_stale_entry_as_evidence_but_never_executes(monkeypatch) -> None:
    source_id = uuid4()
    persisted: list[int] = []
    broker_dispatches: list[int] = []

    def persist_evidence(self, captured):
        persisted.append(captured.telegram_message_id)
        return True

    monkeypatch.setattr(
        CanonicalProductionTelegramListenerManager,
        "_persist_recovered_original",
        persist_evidence,
    )
    manager = object.__new__(CanonicalProductionTelegramListenerManager)
    manager._canonical_router = _StoredStaleEntryRouter()
    manager._dispatch_sync = lambda **kwargs: broker_dispatches.append(
        kwargs["telegram_message_id"]
    )
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

    asyncio.run(manager._recover_live_gaps(_CatchupClient([message]), plan))

    assert persisted == [37003]
    assert broker_dispatches == []


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

    restarted_service = _Day36RestartHarness(shared)
    second = asyncio.run(restarted_service.reconcile(USER))

    assert second.stop_loss_changes == 0
    assert second.take_profit_changes == 0
    assert shared["stop_loss"] == Decimal("4397.5")
    assert shared["take_profit"] == Decimal("4422.25")
    assert restarted_service.actions == []
    assert second.broker_trade_action_created is False
