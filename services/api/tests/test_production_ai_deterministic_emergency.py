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
    policy_text = ProductionAiMessagePipeline._policy_text(text, None)
    return apply_v1_message_policy(decision, raw_text=policy_text)


def test_tig_gold_now_with_price_is_safe_bare_now_trade() -> None:
    decision = _fallback("BUY GOLD NOW  4125")
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


_GOLD_PIPS_59216 = """GOLD BUY NOW

ENTRY ZONE: 4075 – 4070

TP 1: 4077
TP 2: 4087

SL: 4065"""


def test_gold_pips_multiline_entry_zone_uses_only_current_message() -> None:
    decision = _fallback(_GOLD_PIPS_59216)

    assert decision is not None
    assert decision.action == "execute"
    assert decision.extracted["side"] == "BUY"
    assert decision.extracted["entry_low"] == "4070"
    assert decision.extracted["entry_high"] == "4075"
    assert decision.extracted["stop_loss"] == "4065"
    assert decision.extracted["take_profits"] == ["4077", "4087"]


def test_gold_pips_entry_zone_rejects_incomplete_or_wrong_side_geometry() -> None:
    assert _fallback(_GOLD_PIPS_59216.replace("SL: 4065", "")) is None
    assert _fallback(_GOLD_PIPS_59216.replace("ENTRY ZONE: 4075 – 4070", "4075 – 4070")) is None
    decision = _fallback(_GOLD_PIPS_59216.replace("BUY", "SELL"))
    assert decision is not None
    assert decision.action != "execute"


def test_compact_gold_sniper_zone_has_strict_deterministic_path() -> None:
    text = "XAUUSD BUY 4119-17\nTP1: 4124\nTP2: 4129\nSL: 4107"
    decision = _fallback(text)
    assert decision is not None
    assert decision.action == "execute"
    assert decision.extracted["entry_low"] == "4117"
    assert decision.extracted["entry_high"] == "4119"
    assert _fallback(text.replace("4119-17", "4102-99")) is None
