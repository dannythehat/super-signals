from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.critical_entry_policy import parse_critical_entries
from app.mt5_execution_day26 import Day26Mt5ExecutionService
from app.paper_fresh_start_execution import PaperFreshStartExecutionService
from app.telegram_listener_day21 import Day21TelegramListenerManager
from app.telegram_listener_canonical import CanonicalProductionTelegramListenerManager


def test_ordinary_market_range_is_not_converted_into_fake_pending_layers() -> None:
    entries = parse_critical_entries(
        "Buy Gold @4395-4385\n\nSL: 4380\n\n"
        "TP1: 4399\nTP2: 4405\n\nEnter Slowly - Layer with proper money management",
        side="BUY",
        entry_low=Decimal("4385"),
        entry_high=Decimal("4395"),
    )
    assert entries == ()


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
        CanonicalProductionTelegramListenerManager._recover_live_gaps(
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
        self._canonical_router = _UnresolvedRouter()


def test_recovered_unresolved_management_is_not_repeatedly_dispatched() -> None:
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
        PaperFreshStartExecutionService._rollback_critical(
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
