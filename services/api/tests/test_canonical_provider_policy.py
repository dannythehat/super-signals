from __future__ import annotations

from app.ai_message_supervisor import AiMessageDecision
from app.critical_entry_policy import augment_management_actions
from app.v1_message_policy import apply_v1_message_policy


def _decision(**extracted):
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=0.99,
        reason="test",
        extracted=extracted,
        model="test",
        response_id=None,
        latency_ms=0,
        source="test",
        raw_text_sha256="0" * 64,
    )


def test_tdc_6674_dedicated_gold_source_is_not_killed_for_omitted_instrument() -> None:
    raw = """Asia Buy Limit

4311 - 4307

TP 4314
TP 4317
TP 4320

SL 4303"""
    decision = _decision(
        symbol="GOLD",
        side="BUY",
        order_type="pending",
        entry_low="4307",
        entry_high="4311",
        stop_loss="4303",
        take_profits=["4314", "4317", "4320"],
        double_lot=False,
        source_profile="tdc_xauusd",
    )

    result = apply_v1_message_policy(decision, raw_text=raw)

    assert result.action == "execute"
    assert result.reason == "v1_complete_pending_signal"
    assert result.extracted["symbol"] == "XAUUSD"
    assert result.extracted["entry_low"] == "4307"
    assert result.extracted["entry_high"] == "4311"
    assert result.extracted["entry_plan"] == [
        {"entry_index": 1, "order_type": "buy_limit", "price": "4311"}
    ]


def test_plain_range_cannot_be_converted_to_pending_by_ai_guess() -> None:
    raw = "BUY GOLD 4401 - 4396\nSL 4390\nTP 4410"
    decision = _decision(
        symbol="GOLD",
        side="BUY",
        order_type="pending",
        entry_low="4396",
        entry_high="4401",
        stop_loss="4390",
        take_profits=["4410"],
        double_lot=False,
    )

    result = apply_v1_message_policy(decision, raw_text=raw)

    assert result.action == "execute"
    assert result.reason == "v1_complete_zone_signal"
    assert result.extracted["order_type"] == "market"


def test_best_entry_still_running_is_state_not_implicit_close_command() -> None:
    actions = augment_management_actions("BEST ENTRY STILL RUNNING", ())
    assert not any(
        action.get("type") == "close" and action.get("target") == "all_but_best"
        for action in actions
    )
