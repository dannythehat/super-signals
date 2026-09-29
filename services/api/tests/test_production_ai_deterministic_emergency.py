from __future__ import annotations

from app.production_ai_pipeline import ProductionAiMessagePipeline
from app.v1_message_policy import apply_v1_message_policy


def _fallback(text: str, *, source_status: str = "testing"):
    decision = ProductionAiMessagePipeline._deterministic_complete_gold_trade(
        text,
        source_status=source_status,
    )
    if decision is None:
        return None
    return apply_v1_message_policy(decision, raw_text=text)


def test_tig_gold_now_with_price_is_safe_bare_now_trade() -> None:
    decision = ProductionAiMessagePipeline._deterministic_complete_gold_trade(
        "BUY GOLD NOW  4125",
        source_status="testing",
    )
    assert decision is not None
    assert decision.decision == "new_trade"
    assert decision.action == "execute"
    assert decision.extracted["symbol"] == "XAUUSD"
    assert decision.extracted["side"] == "BUY"
    assert decision.extracted["execution_profile"] == "bare_gold_now_50_100"


def test_sureshot_complete_signal_executes_without_supervisor() -> None:
    text = "XAUUSD BUY 4125.74\n\nSL:  4115.74\nTP:  4145.74\n--Trade by Liam"
    decision = _fallback(text)
    assert decision is not None
    assert decision.action == "execute"
    assert decision.extracted["entry_low"] == "4125.74"
    assert decision.extracted["stop_loss"] == "4115.74"
    assert decision.extracted["take_profits"] == ["4145.74"]


def test_numbered_superscript_targets_execute_without_supervisor() -> None:
    text = (
        "#XAUUSD SELL 4127\n\n"
        "TP¹  4122\nTP²  4117\nTP³  4112\nTP⁴ 4107\nTP⁵ Open\n\n"
        "Stop loss 4140"
    )
    decision = _fallback(text)
    assert decision is not None
    assert decision.action == "execute"
    assert decision.extracted["side"] == "SELL"
    assert decision.extracted["take_profits"] == ["4122", "4117", "4112", "4107"]
    assert decision.extracted["tp_open"] is True


def test_incomplete_or_foreign_signal_stays_fail_closed() -> None:
    assert _fallback("XAUUSD BUY 4125") is None
    assert _fallback("BTCUSD BUY 4125\nSL 4115\nTP 4145") is None
    assert _fallback("XAUUSD BUY 4125\nSL 4115\nTP 4145", source_status="shadow") is None
