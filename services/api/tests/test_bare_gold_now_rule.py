from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import app.v1_message_policy as v1
from app.ai_message_supervisor import AiMessageDecision
from app.bare_gold_now_policy import (
    PROFILE,
    STOP_LOSS_DISTANCE,
    TAKE_PROFIT_DISTANCE,
    bare_now_side,
)
from app.mt5_execution_day26 import _SignalInput
from app.trading_execution_canonical import CanonicalTradingExecutionService


def _decision(*, side: str = "BUY") -> AiMessageDecision:
    return AiMessageDecision(
        decision="new_trade",
        action="skip",
        confidence=0.99,
        reason="provider_instruction_incomplete",
        extracted={
            "symbol": "XAUUSD",
            "side": side,
            "order_type": "market",
            "entry_low": None,
            "entry_high": None,
            "stop_loss": None,
            "take_profits": [],
            "double_lot": False,
            "update_type": None,
            "update_target": None,
            "update_value": None,
            "provider_claimed_pips": None,
        },
        model="test",
        response_id=None,
        latency_ms=1,
        source="openai",
        raw_text_sha256="x",
    )


def test_bare_now_matcher_is_intentionally_narrow() -> None:
    assert bare_now_side("BUY GOLD NOW") == "BUY"
    assert bare_now_side("Gold sell now🔥") == "SELL"
    assert bare_now_side("SELL XAUUSD NOW") == "SELL"
    assert bare_now_side("BUY GOLD NOW\nSL 4380\nTP 4400") is None
    assert bare_now_side("BUY GOLD NOW 4390") is None
    assert bare_now_side("GET READY TO BUY GOLD NOW") is None


def test_bare_buy_gold_now_becomes_special_market_profile() -> None:
    result = v1.apply_v1_message_policy(_decision(side="BUY"), raw_text="Buy Gold Now")
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == PROFILE
    assert result.extracted["execution_profile"] == PROFILE
    assert result.extracted["entry_low"] is None
    assert result.extracted["entry_high"] is None
    assert result.extracted["stop_loss"] is None
    assert result.extracted["take_profits"] == []


def test_bare_now_current_revision_is_not_blocked_only_because_it_is_an_edit() -> None:
    result = v1.apply_v1_message_policy(
        _decision(side="BUY"),
        raw_text="Buy Gold Now",
        is_edit=True,
        original_has_signal=True,
    )
    assert result.action == "execute"
    assert result.reason == PROFILE
    assert result.extracted["execution_profile"] == PROFILE


def test_normal_complete_signal_is_not_rewritten_as_bare_now() -> None:
    decision = _decision(side="BUY")
    decision.extracted.update(
        {
            "stop_loss": "4380",
            "take_profits": ["4400"],
        }
    )
    raw = "BUY GOLD NOW\nSL 4380\nTP 4400"
    result = v1.apply_v1_message_policy(decision, raw_text=raw)
    assert result.reason != PROFILE
    assert result.extracted.get("execution_profile") != PROFILE
    assert result.extracted["stop_loss"] == "4380"
    assert result.extracted["take_profits"] == ["4400"]


class _ScalarResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self, raw_text: str) -> None:
        self._raw_text = raw_text

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return _ScalarResult(self._raw_text)


class _SessionFactory:
    def __init__(self, raw_text: str) -> None:
        self._raw_text = raw_text

    def __call__(self):
        return _Session(self._raw_text)


def _signal(side: str) -> _SignalInput:
    return _SignalInput(
        signal_id=uuid4(),
        symbol="XAUUSD",
        side=side,
        entry_low=Decimal("0"),
        entry_high=Decimal("0"),
        stop_loss=Decimal("0"),
        take_profits=(),
        has_open_runner=False,
        signal_requests_double_lot=False,
        source_revision_index=0,
        source_posted_at=datetime.now(UTC),
    )


def test_bare_buy_derives_50_tp_and_100_sl_from_live_ask() -> None:
    service = object.__new__(CanonicalTradingExecutionService)
    service._session_factory = _SessionFactory("BUY GOLD NOW")
    service._paper_max_signal_age_seconds = 90.0
    signal = _signal("BUY")
    state = SimpleNamespace(
        execution_ready=True,
        execution_block_reason=None,
        price=SimpleNamespace(ask=Decimal("4390"), bid=Decimal("4389.5")),
    )
    entry, _ = asyncio.run(
        service._resolve_entry(
            owner_user_id=uuid4(),
            signal=signal,
            day23=SimpleNamespace(),
            initial_state=state,
        )
    )
    assert entry == Decimal("4390")
    assert signal.stop_loss == entry - STOP_LOSS_DISTANCE
    assert signal.take_profits == (entry + TAKE_PROFIT_DISTANCE,)


def test_bare_sell_derives_50_tp_and_100_sl_from_live_bid() -> None:
    service = object.__new__(CanonicalTradingExecutionService)
    service._session_factory = _SessionFactory("SELL GOLD NOW")
    service._paper_max_signal_age_seconds = 90.0
    signal = _signal("SELL")
    state = SimpleNamespace(
        execution_ready=True,
        execution_block_reason=None,
        price=SimpleNamespace(ask=Decimal("4390.5"), bid=Decimal("4390")),
    )
    entry, _ = asyncio.run(
        service._resolve_entry(
            owner_user_id=uuid4(),
            signal=signal,
            day23=SimpleNamespace(),
            initial_state=state,
        )
    )
    assert entry == Decimal("4390")
    assert signal.stop_loss == entry + STOP_LOSS_DISTANCE
    assert signal.take_profits == (entry - TAKE_PROFIT_DISTANCE,)
