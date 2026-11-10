"""Regression: provider literal 4119-17 must not become a missed trade.

Only the original message supplies numerical evidence. The explicit zone
must never be flattened into a market price, and historical signals are
not replayed by these tests.
"""

from decimal import Decimal

from app.ai_message_supervisor import AiMessageDecision, _guard_execute_decision, _literal_numbers
from app.v1_message_policy import apply_v1_message_policy


RAW = "XAUUSD BUY 4119-17\\nTP 1 : 4124\\nTP 2 : 4129\\nTP 3 : 4134\\nTP 4 : 4154\\nSL : 4107"


def _decision(raw=RAW):
    return AiMessageDecision(
        decision="new_trade", action="execute", confidence=0.99, reason="explicit",
        extracted={
            "symbol": "XAUUSD", "side": "BUY", "order_type": "market",
            "entry_low": "4117", "entry_high": "4119", "stop_loss": "4107",
            "take_profits": ["4124", "4129", "4134", "4154"],
            "double_lot": False, "update_type": None, "update_target": None,
            "update_value": None, "provider_claimed_pips": None
        },
        model="fixture", response_id=None, latency_ms=0, source="test",
        raw_text_sha256="0"*64
    )


def test_gold_sniper_short_range_is_literal_evidence():
    values = _literal_numbers(RAW)
    assert Decimal("4117") in values and Decimal("4119") in values
    assert Decimal("4107") in values


def test_gold_sniper_zone_can_pass_both_execution_gates():
    raw = RAW.replace("\\n", "\n")
    ai_guard = _guard_execute_decision({**_decision().extracted, "decision": "new_trade", "action": "execute"}, raw)
    # The early supervisor still refuses ranges; the canonical policy handles
    # supported complete zones after verifying all literal price evidence.
    assert ai_guard["action"] == "skip"
    assert ai_guard["reason"] == "unsupported_entry_range"
    policy = apply_v1_message_policy(_decision(), raw_text=raw)
    assert policy.action == "execute"
    assert policy.extracted["entry_low"] == "4117"
    assert policy.extracted["entry_high"] == "4119"


def test_cross_hundred_short_range_not_fabricated():
    values = _literal_numbers("XAUUSD BUY 4102-99\\nSL 4090\\nTP1 4120")
    assert Decimal("4099") not in values
    assert Decimal("4199") not in values


def test_missing_stop_never_promoted():
    raw = "XAUUSD BUY 4119-17\\nTP1 4124"
    policy = apply_v1_message_policy(_decision(), raw_text=raw.replace("\\n", "\n"))
    assert policy.action == "skip"


def test_wrong_target_not_in_original_is_rejected():
    raw = "XAUUSD BUY 4119-17\\nTP1 4124\\nTP2 4129\\nTP3 4134\\nTP4 4155\\nSL 4107"
    policy = apply_v1_message_policy(_decision(), raw_text=raw.replace("\\n", "\n"))
    assert policy.action == "skip"
