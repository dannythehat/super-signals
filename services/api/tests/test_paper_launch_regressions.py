import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from telethon.errors import FloodWaitError

from app.ai_message_supervisor import AiMessageDecision
from app.mt5_connection_manager import Mt5ConnectionManager
from app.telegram_listener import ListeningSource, ReaderListeningPlan
from app.telegram_listener_day21 import Day21TelegramListenerManager
from app.telegram_listener_day28 import Day28TelegramListenerManager
from app.v1_message_policy import apply_v1_message_policy


def _fx_double_lot_decision() -> AiMessageDecision:
    raw = "fixture"
    return AiMessageDecision(
        decision="new_trade",
        action="skip",
        confidence=0.99,
        reason="ai_fixture",
        extracted={
            "symbol": "XAUUSD",
            "side": "SELL",
            "order_type": "market",
            "entry_low": "4386",
            "entry_high": "4386",
            "stop_loss": "4410",
            "take_profits": ["4382", "4381", "4350"],
            "double_lot": False,
            "update_type": None,
            "update_target": None,
            "update_value": None,
            "provider_claimed_pips": None,
        },
        model="fixture",
        response_id=None,
        latency_ms=1,
        source="openai",
        raw_text_sha256=sha256(raw.encode()).hexdigest(),
    )


def test_fxtradingvision_double_lotsize_literal_executes_double() -> None:
    raw = (
        "NEW TRADE IDEA\n\n"
        "XAUUSD SELL 4386\n\n"
        "TP 1 4382\n"
        "TP 2 4381\n"
        "TP 3 4350\n\n"
        "SL @ 4410\n\n"
        "Trade accordingly and only trade with money you can afford to LOSE.\n\n"
        "USE DOUBLE LOTSIZE"
    )
    result = apply_v1_message_policy(_fx_double_lot_decision(), raw_text=raw)
    assert result.action == "execute"
    assert result.reason == "v1_complete_exact_signal"
    assert result.extracted["double_lot"] is True


def test_live_gap_recovery_freshness_is_bounded() -> None:
    now = datetime.now(UTC)
    assert Day28TelegramListenerManager._fresh_for_live_recovery(
        now - timedelta(seconds=30), now=now
    )
    assert not Day28TelegramListenerManager._fresh_for_live_recovery(
        now - timedelta(seconds=61), now=now
    )


def test_marginal_clock_skew_does_not_discard_a_genuinely_fresh_delivery() -> None:
    # A provider timestamp a second or two ahead of this server's clock is
    # ordinary skew on a just-posted message, not a stale replay.
    now = datetime.now(UTC)
    assert Day28TelegramListenerManager._fresh_for_live_recovery(
        now + timedelta(seconds=2), now=now
    )


def test_implausible_future_timestamp_stays_evidence_only() -> None:
    # Skew tolerance is deliberately narrow. A wildly future-dated message is
    # not trusted into the broker path.
    now = datetime.now(UTC)
    assert not Day28TelegramListenerManager._fresh_for_live_recovery(
        now + timedelta(seconds=120), now=now
    )


@pytest.mark.asyncio
async def test_already_persisted_message_is_never_dispatched_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message the live push already stored must not be re-sent to the broker.

    _persist_message returns False when the unique (source, telegram_message_id)
    key already exists, which is the structural guarantee against the recovery
    sweep duplicating an order for a message push delivered normally.
    """
    source = ListeningSource(source_id=uuid4(), chat_id=-100123, title="Fixture")
    plan = ReaderListeningPlan(
        telegram_account_id=uuid4(),
        session_ciphertext=b"fixture",
        sources=(source,),
    )
    now = datetime.now(UTC)
    messages = [
        SimpleNamespace(
            id=102,
            raw_text="SELL XAUUSD 4386\nSL 4410\nTP 4382",
            date=now - timedelta(seconds=5),
            edit_date=None,
            reply_to=None,
            media=None,
        ),
    ]

    class FakeClient:
        async def get_messages(self, chat_id: int, limit: int):
            return messages

    dispatched: list[int] = []

    # Already seen: the insert loses the unique-key race and reports False.
    monkeypatch.setattr(
        Day21TelegramListenerManager, "_persist_message", lambda _self, _c: False
    )

    manager = object.__new__(Day28TelegramListenerManager)
    manager._dispatch_sync = lambda **kwargs: dispatched.append(kwargs["telegram_message_id"])

    await manager._recover_live_gaps(FakeClient(), plan)

    assert dispatched == []


@pytest.mark.asyncio
async def test_live_recovery_backs_off_when_telegram_asks_for_flood_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flood wait must widen the gap between sweeps, not be retried on schedule."""
    plan = ReaderListeningPlan(
        telegram_account_id=uuid4(),
        session_ciphertext=b"fixture",
        sources=(ListeningSource(source_id=uuid4(), chat_id=-100123, title="Fixture"),),
    )
    slept: list[float] = []
    connected = {"value": True}

    class FakeClient:
        def is_connected(self) -> bool:
            # Run exactly one recovery pass, then end the loop.
            if not connected["value"]:
                return False
            return True

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 2:
            connected["value"] = False

    async def raise_flood(_self, _client, _plan) -> None:
        raise FloodWaitError(request=None)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        Day21TelegramListenerManager, "_recover_live_gaps", raise_flood
    )

    manager = object.__new__(Day28TelegramListenerManager)
    await Day21TelegramListenerManager._run_live_recovery(manager, FakeClient(), plan)

    # First sleep is the normal interval; the second is the enforced backoff.
    assert slept[0] == 15
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_fresh_missing_telegram_post_is_dispatched_but_stale_one_is_evidence_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ListeningSource(source_id=uuid4(), chat_id=-100123, title="Fixture")
    plan = ReaderListeningPlan(
        telegram_account_id=uuid4(),
        session_ciphertext=b"fixture",
        sources=(source,),
    )
    now = datetime.now(UTC)
    # Telethon recent history is newest first; recovery reverses it so persistence
    # and any fresh dispatch happen oldest to newest.
    messages = [
        SimpleNamespace(
            id=102,
            raw_text="SELL XAUUSD 4386\nSL 4410\nTP 4382",
            date=now - timedelta(seconds=5),
            edit_date=None,
            reply_to=None,
            media=None,
        ),
        SimpleNamespace(
            id=101,
            raw_text="BUY XAUUSD 4380\nSL 4370\nTP 4390",
            date=now - timedelta(seconds=90),
            edit_date=None,
            reply_to=None,
            media=None,
        ),
    ]

    class FakeClient:
        async def get_messages(self, chat_id: int, limit: int):
            assert chat_id == source.chat_id
            assert limit == 10
            return messages

    persisted: list[int] = []
    dispatched: list[int] = []

    def fake_persist(_self, captured):
        persisted.append(captured.telegram_message_id)
        return True

    monkeypatch.setattr(Day21TelegramListenerManager, "_persist_message", fake_persist)

    manager = object.__new__(Day28TelegramListenerManager)
    manager._dispatch_sync = lambda **kwargs: dispatched.append(kwargs["telegram_message_id"])

    await manager._recover_live_gaps(FakeClient(), plan)

    assert persisted == [101, 102]
    assert dispatched == [102]


@pytest.mark.asyncio
async def test_slow_broker_startup_cannot_block_application_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled MetaAPI must not hold the FastAPI lifespan open indefinitely.

    The startup reconciliation is bounded; on timeout the service still finishes
    starting and the periodic monitor retries. Nothing is reported as reconciled.
    """
    started: list[str] = []

    class StalledService:
        async def reconcile_all(self) -> int:
            await asyncio.sleep(3600)
            return 1

    manager = Mt5ConnectionManager(StalledService())
    monkeypatch.setattr(
        "app.mt5_connection_manager._STARTUP_RECONCILE_TIMEOUT_SECONDS", 0.05
    )
    monkeypatch.setattr(
        Mt5ConnectionManager, "_run", lambda self: _noop(started)
    )

    await asyncio.wait_for(manager.start(), timeout=5)

    # Startup completed despite the broker never answering, and the background
    # monitor is running so reconciliation is retried rather than abandoned.
    assert manager._task is not None
    await manager.stop()


async def _noop(started: list[str]) -> None:
    started.append("running")
    await asyncio.sleep(3600)
