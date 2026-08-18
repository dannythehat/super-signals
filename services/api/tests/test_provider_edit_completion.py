from __future__ import annotations

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy


def _trade_decision(**extracted) -> AiMessageDecision:
    defaults = {
        "symbol": "XAUUSD",
        "side": "BUY",
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
    }
    defaults.update(extracted)
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=0.99,
        reason="model_classification",
        extracted=defaults,
        model="test",
        response_id=None,
        latency_ms=1,
        source="openai",
        raw_text_sha256="x",
    )


def test_tdc_complete_trade_edit_can_create_first_signal() -> None:
    previous_text = """Buy Gold Now

4396.5 - 4391.5

TP 4399"""
    raw_text = """Buy Gold Now

4396.5 - 4391.5

TP 4399
TP 4401
TP 4404
TP 4408

SL 4388"""
    decision = _trade_decision(
        entry_low="4391.5",
        entry_high="4396.5",
        stop_loss="4388",
        take_profits=["4399", "4401", "4404", "4408"],
    )
    result = apply_v1_message_policy(
        decision,
        raw_text=raw_text,
        is_edit=True,
        original_has_signal=False,
        previous_text=previous_text,
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_zone_signal_from_structured_edit"


def test_incomplete_trade_edit_still_fails_closed() -> None:
    previous_text = """Buy Gold Now

4396.5 - 4391.5"""
    raw_text = """Buy Gold Now

4396.5 - 4391.5

TP 4399"""
    decision = _trade_decision(
        entry_low="4391.5",
        entry_high="4396.5",
        take_profits=["4399"],
    )
    result = apply_v1_message_policy(
        decision,
        raw_text=raw_text,
        is_edit=True,
        original_has_signal=False,
        previous_text=previous_text,
    )
    assert result.action == "skip"
    assert result.reason == "missing_sl"
