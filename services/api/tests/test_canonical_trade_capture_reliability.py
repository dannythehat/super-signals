from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.critical_entry_policy import parse_critical_entries
from app.mt5_execution_day26 import Day26Mt5ExecutionService
from app.telegram_listener_canonical import CanonicalProductionTelegramListenerManager
from app.trading_execution_canonical import CanonicalTradingExecutionService


def test_ordinary_market_range_is_not_converted_into_fake_pending_layers() -> None:
    entries = parse_critical_entries(
        "Buy Gold @4395-4385\n\nSL: 4380\n\n"
        "TP1: 4399\nTP2: 4405\n\nEnter Slowly - Layer with proper money management",
        side="BUY",
        entry_low=Decimal("4385"),
        entry_high=Decimal("4395"),
    )
    assert entries == ()


def test_united_kings_valid_sell_range_is_market_and_passes_directional_safety() -> None:
    raw = (
        "Sell Gold @4363-4373\n\n"
        "SL: 4377\n\n"
        "TP1: 4358\nTP2: 4355\n\n"
        "Enter slowly — layer your entries with proper risk management.\n\n"
        "Do not rush entries."
    )
    entries = parse_critical_entries(
        raw,
        side="SELL",
        entry_low=Decimal("4363"),
        entry_high=Decimal("4373"),
    )
    assert entries == ()
    assert Day26Mt5ExecutionService._directionally_valid(
        side="SELL",
        entry_low=Decimal("4363"),
        entry_high=Decimal("4373"),
        stop_loss=Decimal("4377"),
        take_profits=(Decimal("4358"), Decimal("4355")),
    )


def test_four_numeric_take_profits_are_valid_execution_input() -> None:
    values = Day26Mt5ExecutionService._take_profits(["4396.5", "4399", "4402", "4413"])
    assert len(values) == 4
    assert str(values[-1]) == "4413"


class _RecoveryClient:
    def __init__(self, message) -> None:
        self.message = message

    async def get_messages(self, chat_id, limit):
        assert limit == 50
        return [self.message]


def _bare_listener() -> CanonicalProductionTelegramListenerManager:
    manager = object.__new__(CanonicalProductionTelegramListenerManager)
    manager._telegram_revision_locks = tuple(RLock() for _ in range(128))
    manager._ai_pipeline = None
    return manager


def test_live_recovery_rechecks_already_persisted_fresh_original_for_missed_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_calls: list[int] = []

    def already_persisted(self, captured):
        persisted_calls.append(captured.telegram_message_id)
        return False

    monkeypatch.setattr(
        CanonicalProductionTelegramListenerManager,
        "_persist_recovered_original",
        already_persisted,
    )

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
    manager = _bare_listener()
    dispatched: list[tuple[int, int]] = []

    async def record_dispatch(**kwargs):
        dispatched.append((kwargs["telegram_message_id"], kwargs["revision_index"]))

    manager._dispatch_recovered_if_required = record_dispatch

    asyncio.run(manager._recover_live_gaps(_RecoveryClient(message), plan))

    assert persisted_calls == [6503]
    assert dispatched == [(6503, 0)]


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
        self._canonical_router = _UnresolvedRouter()

    @staticmethod
    def _fresh_recovered_entry(value, *, now=None):
        return CanonicalProductionTelegramListenerManager._fresh_recovered_entry(value, now=now)


def test_recovered_unresolved_management_is_not_dispatched() -> None:
    harness = _DispatchHarness()
    asyncio.run(
        CanonicalProductionTelegramListenerManager._dispatch_recovered_if_required(
            harness,
            source_id=uuid4(),
            telegram_message_id=6520,
            revision_index=0,
            occurred_at=datetime.now(UTC),
        )
    )
    assert harness._canonical_router.resolve_calls == 1


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
    local_id = uuid4()
    client_id = "SS_abcdef123456_E1T1"
    planned = (SimpleNamespace(local_id=local_id, client_id=client_id),)
    harness = _TimeoutRollbackHarness(client_id)

    complete = asyncio.run(
        CanonicalTradingExecutionService._rollback_critical(
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



def test_recovered_entry_has_route_and_supersession_guards() -> None:
    from pathlib import Path

    source = Path("services/api/app/telegram_listener_canonical.py").read_text()
    assert "_entry_route_already_attempted" in source
    assert "event_type='mt5.day38_route_new_trade'" in source
    assert "_entry_superseded_by_newer_signal" in source
    assert "newer.created_at>current.created_at" in source
    assert "if is_entry:" in source
