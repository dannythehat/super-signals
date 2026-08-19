import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import RLock
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from telethon.errors import FloodWaitError

from app.ai_message_supervisor import AiMessageDecision
from app.mt5_connection_manager import Mt5ConnectionManager
from app.telegram_listener import ListeningSource, ReaderListeningPlan
from app.telegram_listener_day21 import Day21TelegramListenerManager
from app.telegram_listener_canonical import CanonicalProductionTelegramListenerManager
from app.v1_message_policy import apply_v1_message_policy


def _bare_listener() -> CanonicalProductionTelegramListenerManager:
    """Construct only the listener state required by isolated unit tests."""
    manager = object.__new__(CanonicalProductionTelegramListenerManager)
    manager._telegram_revision_locks = tuple(RLock() for _ in range(128))
    return manager


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


def test_live_gap_recovery_freshness_uses_canonical_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_MAX_SIGNAL_AGE_SECONDS", "90")
    now = datetime.now(UTC)
    assert CanonicalProductionTelegramListenerManager._fresh_recovered_entry(
        now - timedelta(seconds=30), now=now
    )
    assert CanonicalProductionTelegramListenerManager._fresh_recovered_entry(
        now - timedelta(seconds=89), now=now
    )
    assert not CanonicalProductionTelegramListenerManager._fresh_recovered_entry(
        now - timedelta(seconds=91), now=now
    )


def test_marginal_clock_skew_does_not_discard_a_genuinely_fresh_delivery() -> None:
    now = datetime.now(UTC)
    assert CanonicalProductionTelegramListenerManager._fresh_recovered_entry(
        now + timedelta(seconds=2), now=now
    )


def test_implausible_future_timestamp_stays_evidence_only() -> None:
    now = datetime.now(UTC)
    assert not CanonicalProductionTelegramListenerManager._fresh_recovered_entry(
        now + timedelta(seconds=120), now=now
    )


@pytest.mark.asyncio
async def test_live_recovery_backs_off_when_telegram_asks_for_flood_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ReaderListeningPlan(
        telegram_account_id=uuid4(),
        session_ciphertext=b"fixture",
        sources=(ListeningSource(source_id=uuid4(), chat_id=-100123, title="Fixture"),),
    )
    slept: list[float] = []
    connected = {"value": True}

    class FakeClient:
        def is_connected(self) -> bool:
            return connected["value"]

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 2:
            connected["value"] = False

    async def raise_flood(_self, _client, _plan) -> None:
        raise FloodWaitError(request=None)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        CanonicalProductionTelegramListenerManager,
        "_recover_live_gaps",
        raise_flood,
    )

    manager = _bare_listener()
    await Day21TelegramListenerManager._run_live_recovery(manager, FakeClient(), plan)

    assert slept[0] == 15
    assert len(slept) == 2


class _StoredNewTradeRouter:
    def _load_stored_decision(self, **kwargs):
        return SimpleNamespace(
            message_id=uuid4(),
            decision="new_trade",
            action="execute",
        )


@pytest.mark.asyncio
async def test_fresh_missing_post_is_routed_but_stale_post_is_evidence_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        SimpleNamespace(
            id=101,
            raw_text="BUY XAUUSD 4380\nSL 4370\nTP 4390",
            date=now - timedelta(seconds=120),
            edit_date=None,
            reply_to=None,
            media=None,
        ),
    ]

    class FakeClient:
        async def get_messages(self, chat_id: int, limit: int):
            assert chat_id == source.chat_id
            assert limit == 50
            return messages

    persisted: list[int] = []
    dispatched: list[int] = []

    def fake_persist(_self, captured):
        persisted.append(captured.telegram_message_id)
        return True

    monkeypatch.setattr(
        CanonicalProductionTelegramListenerManager,
        "_persist_recovered_original",
        fake_persist,
    )

    manager = _bare_listener()
    manager._canonical_router = _StoredNewTradeRouter()
    manager._ai_pipeline = None
    manager._dispatch_sync = lambda **kwargs: dispatched.append(kwargs["telegram_message_id"])

    await manager._recover_live_gaps(FakeClient(), plan)

    assert persisted == [101, 102]
    assert dispatched == [102]


@pytest.mark.asyncio
async def test_one_unreadable_source_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = ListeningSource(source_id=uuid4(), chat_id=-1001821216397, title="Unreadable")
    good = ListeningSource(source_id=uuid4(), chat_id=-100999, title="Readable")
    plan = ReaderListeningPlan(
        telegram_account_id=uuid4(), session_ciphertext=b"fixture", sources=(bad, good)
    )
    now = datetime.now(UTC)

    class FakeClient:
        async def get_messages(self, chat_id: int, limit: int):
            if chat_id == bad.chat_id:
                raise ValueError(
                    "Could not find the input entity for PeerChannel(channel_id=1821216397)"
                )
            return [
                SimpleNamespace(
                    id=900,
                    raw_text="SELL XAUUSD 4386\nSL 4410\nTP 4382",
                    date=now - timedelta(seconds=5),
                    edit_date=None,
                    reply_to=None,
                    media=None,
                )
            ]

        async def iter_dialogs(self):
            if False:
                yield None

    persisted: list[int] = []
    dispatched: list[int] = []

    def fake_persist(_self, captured):
        persisted.append(captured.telegram_message_id)
        return True

    monkeypatch.setattr(
        CanonicalProductionTelegramListenerManager,
        "_persist_recovered_original",
        fake_persist,
    )

    manager = _bare_listener()
    manager._ai_pipeline = None

    async def record_dispatch(self, **kwargs):
        dispatched.append(kwargs["telegram_message_id"])

    manager._dispatch_recovered_if_required = MethodType(record_dispatch, manager)
    await manager._recover_live_gaps(FakeClient(), plan)

    assert persisted == [900]
    assert dispatched == [900]


@pytest.mark.asyncio
async def test_slow_broker_startup_cannot_block_application_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []

    class StalledService:
        async def reconcile_all(self) -> int:
            await asyncio.sleep(3600)
            return 1

    manager = Mt5ConnectionManager(StalledService())
    monkeypatch.setattr(
        "app.mt5_connection_manager._STARTUP_RECONCILE_TIMEOUT_SECONDS", 0.05
    )
    monkeypatch.setattr(Mt5ConnectionManager, "_run", lambda self: _noop(started))

    await asyncio.wait_for(manager.start(), timeout=5)
    assert manager._task is not None
    await manager.stop()


async def _noop(started: list[str]) -> None:
    started.append("running")
    await asyncio.sleep(3600)


def _tig_complete_decision() -> AiMessageDecision:
    raw = "fixture"
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=0.99,
        reason="ai_fixture",
        extracted={
            "symbol": "XAUUSD",
            "side": "BUY",
            "order_type": "market",
            "entry_low": "4382",
            "entry_high": "4382",
            "stop_loss": "4368",
            "take_profits": ["4388", "4393", "4398"],
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


_TIG_EDITED_RAW = (
    "BUY XAUUSD\n"
    "Entry 4382\n"
    "SL 4368\n"
    "TP1 4388\n"
    "TP2 4393\n"
    "TP3 4398\n"
    "TP4 OPEN"
)


def test_edit_cannot_resurrect_a_skipped_setup_into_a_new_trade() -> None:
    result = apply_v1_message_policy(
        _tig_complete_decision(),
        raw_text=_TIG_EDITED_RAW,
        is_edit=True,
        original_has_signal=False,
    )
    assert result.action == "skip"
    assert result.reason == "edit_cannot_create_first_trade"


def test_edit_enforcement_defaults_to_fail_closed() -> None:
    result = apply_v1_message_policy(
        _tig_complete_decision(),
        raw_text=_TIG_EDITED_RAW,
        is_edit=True,
    )
    assert result.action == "skip"
    assert result.reason == "edit_cannot_create_first_trade"


def test_same_message_still_executes_when_it_is_not_an_edit() -> None:
    result = apply_v1_message_policy(
        _tig_complete_decision(),
        raw_text=_TIG_EDITED_RAW,
        is_edit=False,
    )
    assert result.action == "execute"
    assert result.extracted["tp_open"] is True


def test_edit_may_still_revalidate_a_signal_that_already_exists() -> None:
    result = apply_v1_message_policy(
        _tig_complete_decision(),
        raw_text=_TIG_EDITED_RAW,
        is_edit=True,
        original_has_signal=True,
    )
    assert result.action == "execute"


def test_edit_block_does_not_disturb_lifecycle_management() -> None:
    decision = replace(
        _tig_complete_decision(),
        decision="trade_update",
        action="apply_update",
    )
    result = apply_v1_message_policy(
        decision,
        raw_text="Move SL to 4385",
        is_edit=True,
        original_has_signal=False,
    )
    assert result.decision == "trade_update"
    assert result.reason != "edit_cannot_create_first_trade"


def test_application_logging_exposes_info_diagnostics() -> None:
    import logging as _logging

    from app.main import _configure_logging

    root = _logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    try:
        root.handlers = []
        root.setLevel(_logging.WARNING)
        assert not root.isEnabledFor(_logging.INFO)

        _configure_logging()

        assert root.isEnabledFor(_logging.INFO)
        assert any(getattr(h, "_super_signals", False) for h in root.handlers)

        _configure_logging()
        added = [h for h in root.handlers if getattr(h, "_super_signals", False)]
        assert len(added) == 1
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)


def test_plural_side_words_are_recognised() -> None:
    from app.v1_message_policy import _BUY, _SELL

    assert _SELL.search("OPEN GOLD SELLS") is not None
    assert _SELL.search("OPEN EXTRA GOLD SELLS.") is not None
    assert _BUY.search("OPEN GOLD BUYS") is not None
    assert _SELL.search("RESELLER") is None
    assert _BUY.search("BUYER") is None
