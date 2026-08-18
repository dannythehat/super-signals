from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.day28_zone_guard import Day28ZoneGuardTradeGateway
from app.mt5_execution_day26 import Day26Mt5ExecutionService
from app.paper_critical_execution import PaperCriticalExecutionService
from app.telegram_listener_day21 import Day21TelegramListenerManager
from app.telegram_listener_day38 import PaperPendingAwareListenerManager


class _BaseTrade:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def place_market_order(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(order_id="order-1", position_id="position-1")


class _NoZoneRead:
    async def read_symbol_price(self, **kwargs):
        raise AssertionError("fresh MARKET execution must not be locally re-vetoed by a zone read")


def test_fresh_market_zone_reaches_broker_without_local_price_veto() -> None:
    """Regression for United Kings 28739 / the Aug-18 first-leg zone blocker."""
    base = _BaseTrade()
    gateway = Day28ZoneGuardTradeGateway(base=base, read_gateway=_NoZoneRead())
    token = gateway.set_zone(4385, 4395)
    try:
        result = asyncio.run(
            gateway.place_market_order(
                token="token",
                account_id="account",
                region="london",
                side="BUY",
                symbol="XAUUSD",
                volume=0.01,
                stop_loss=4380,
                take_profit=4399,
                client_id="SS_123456789012_1",
            )
        )
    finally:
        gateway.reset_zone(token)

    assert result.order_id == "order-1"
    assert len(base.calls) == 1
    assert base.calls[0]["stop_loss"] == 4380
    assert base.calls[0]["take_profit"] == 4399


def test_four_numeric_take_profits_are_valid_execution_input() -> None:
    """Regression for TDC 6560, which historically died at four provider TPs."""
    values = Day26Mt5ExecutionService._take_profits(["4396.5", "4399", "4402", "4413"])
    assert len(values) == 4
    assert str(values[-1]) == "4413"


class _RecoveryClient:
    def __init__(self, message) -> None:
        self.message = message

    async def get_messages(self, chat_id, limit):
        assert limit == 50
        return [self.message]


class _RecoveryHarness:
    def __init__(self) -> None:
        self.dispatched: list[tuple[int, int]] = []

    @staticmethod
    def _utc_datetime(value):
        return value

    async def _dispatch_recovered_if_required(
        self, *, source_id, telegram_message_id, revision_index, occurred_at
    ) -> None:
        self.dispatched.append((telegram_message_id, revision_index))

    @staticmethod
    def _latest_revision_index(source_id, telegram_message_id):
        return 0


def test_live_recovery_repairs_already_persisted_unrouted_original(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persisted row is a durable hand-off, not proof broker dispatch occurred."""
    persisted_calls: list[int] = []

    def already_persisted(self, captured):
        persisted_calls.append(captured.telegram_message_id)
        return False

    monkeypatch.setattr(Day21TelegramListenerManager, "_persist_message", already_persisted)

    now = datetime.now(UTC)
    message = SimpleNamespace(
        id=6503,
        reply_to=None,
        media=None,
        raw_text="BUY LIMIT GOLD 4387 4392",
        date=now,
        edit_date=None,
    )
    source = SimpleNamespace(source_id=uuid4(), chat_id=12345)
    plan = SimpleNamespace(sources=(source,))
    harness = _RecoveryHarness()

    asyncio.run(
        PaperPendingAwareListenerManager._recover_live_gaps(
            harness,
            _RecoveryClient(message),
            plan,
        )
    )

    assert persisted_calls == [6503]
    assert harness.dispatched == [(6503, 0)]


class _UnresolvedRouter:
    def __init__(self) -> None:
        self.resolve_calls = 0

    def _load_stored_decision(self, **kwargs):
        return SimpleNamespace(
            message_id=uuid4(),
            decision="trade_update",
            action="apply_update",
        )

    def _resolve_lifecycle_event(self, message_id, revision_index):
        self.resolve_calls += 1
        return None, None


class _DispatchHarness:
    def __init__(self) -> None:
        self._day28_router = _UnresolvedRouter()


def test_recovered_unresolved_management_is_not_repeatedly_dispatched() -> None:
    """Regression for 1,231 Aug-18 lifecycle-resolution route failures."""
    harness = _DispatchHarness()
    asyncio.run(
        PaperPendingAwareListenerManager._dispatch_recovered_if_required(
            harness,
            source_id=uuid4(),
            telegram_message_id=6520,
            revision_index=0,
            occurred_at=datetime.now(UTC),
        )
    )
    assert harness._day28_router.resolve_calls == 1


class _HiddenMutationRead:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id

    async def read_positions(self, **kwargs):
        return [{"id": "hidden-position-1", "clientId": self.client_id}]

    async def read_orders(self, **kwargs):
        return []


class _HiddenMutationTrade:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def close_position(self, *, position_id: str, **kwargs):
        self.closed.append(position_id)

    async def cancel_order(self, **kwargs):
        raise AssertionError("hidden position should be closed, not cancelled")


class _WriteSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *args, **kwargs):
        return None

    def commit(self):
        return None


class _TimeoutRollbackHarness:
    def __init__(self, client_id: str) -> None:
        self._read_gateway = _HiddenMutationRead(client_id)
        self._trade_gateway = _HiddenMutationTrade()
        self._session_factory = lambda: _WriteSession()
        self.audits: list[dict[str, object]] = []

    def _audit(self, **kwargs):
        self.audits.append(kwargs)


def test_timeout_reconciliation_cleans_hidden_unreturned_broker_position() -> None:
    """A timed-out POST is reconciled by clientId; the trade is never retried."""
    local_id = uuid4()
    client_id = "SS_abcdef123456_E1T1"
    planned = (
        SimpleNamespace(local_id=local_id, client_id=client_id),
    )
    harness = _TimeoutRollbackHarness(client_id)

    complete = asyncio.run(
        PaperCriticalExecutionService._rollback_critical(
            harness,
            owner_user_id=uuid4(),
            signal=SimpleNamespace(signal_id=uuid4()),
            account=SimpleNamespace(metaapi_account_id="account"),
            token="token",
            region="london",
            planned=planned,
            submitted={},
            reason="metaapi_timeout",
        )
    )

    assert complete is True
    assert harness._trade_gateway.closed == ["hidden-position-1"]
    assert harness.audits[-1]["payload"]["hidden_mutations_detected"] == 1
    assert harness.audits[-1]["payload"]["automatic_retry"] is False
