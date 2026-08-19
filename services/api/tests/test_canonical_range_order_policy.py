from hashlib import sha256

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy


def _decision(*, order_type: str = "pending") -> AiMessageDecision:
    raw = "fixture"
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=0.99,
        reason="ai_fixture",
        extracted={
            "symbol": "XAUUSD",
            "side": "BUY",
            "order_type": order_type,
            "entry_low": "4396",
            "entry_high": "4401",
            "stop_loss": "4395",
            "take_profits": ["4403", "4406", "4410"],
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


def test_plain_complete_range_executes_even_if_ai_guesses_pending() -> None:
    raw = "BUY GOLD @ 4401/4396\nTP 4403\nTP 4406\nTP 4410\nTP OPEN\nSL 4395"
    result = apply_v1_message_policy(_decision(), raw_text=raw)

    assert result.action == "execute"
    assert result.reason == "v1_complete_zone_signal"
    assert result.extracted["order_type"] == "market"
    assert result.extracted["entry_low"] == "4396"
    assert result.extracted["entry_high"] == "4401"
    assert result.extracted["tp_open"] is True


def test_literal_pending_range_remains_fail_closed_when_structure_is_ambiguous() -> None:
    raw = "BUY LIMITS GOLD 4401/4396\nTP 4403\nTP 4406\nSL 4395"
    result = apply_v1_message_policy(_decision(), raw_text=raw)

    assert result.action == "skip"
    assert result.reason in {
        "pending_layer_grid_unspecified",
        "pending_order_type_ambiguous",
    }
