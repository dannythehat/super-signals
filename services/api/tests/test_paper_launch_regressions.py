from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ai_message_supervisor import AiMessageDecision
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
            # Deliberately false: the mechanical policy must trust the literal
            # provider wording, not require the model to echo this boolean.
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
    messages = [
        SimpleNamespace(
            id=101,
            raw_text="BUY XAUUSD 4380\nSL 4370\nTP 4390",
            date=now - timedelta(seconds=90),
            edit_date=None,
            reply_to=None,
            media=None,
        ),
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
