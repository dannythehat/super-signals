from hashlib import sha256

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy


def _decision(
    *,
    side: str = "BUY",
    entry_low: str = "4396",
    entry_high: str = "4401",
    stop_loss: str = "4395",
    tps: list[str] | None = None,
    order_type: str = "pending",
) -> AiMessageDecision:
    raw = "fixture"
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=0.99,
        reason="ai_fixture",
        extracted={
            "symbol": "XAUUSD",
            "side": side,
            "order_type": order_type,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop_loss,
            "take_profits": tps or ["4403", "4406", "4410"],
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


def test_literal_plural_pending_range_uses_both_stated_prices() -> None:
    raw = "BUY LIMITS GOLD 4401/4396\nTP 4403\nTP 4406\nTP 4410\nSL 4395"
    result = apply_v1_message_policy(_decision(), raw_text=raw)

    assert result.action == "execute"
    assert result.reason == "v1_complete_layered_signal"
    assert result.extracted["order_type"] == "pending"
    assert result.extracted["entry_plan"] == [
        {"entry_index": 1, "order_type": "buy_limit", "price": "4401"},
        {"entry_index": 2, "order_type": "buy_limit", "price": "4396"},
    ]


def test_tdc_sell_limit_entry_price_wording_executes() -> None:
    raw = (
        "Limit Order Asia Trade\n\n"
        "Sell Limit Order\n\n"
        "Entry Price 4541 - 4545\n\n"
        "TP 4538\nTP 4535\nTP 4530\nTP 4505\n\n"
        "SL 4560"
    )
    result = apply_v1_message_policy(
        _decision(
            side="SELL",
            entry_low="4541",
            entry_high="4545",
            stop_loss="4560",
            tps=["4538", "4535", "4530", "4505"],
        ),
        raw_text=raw,
    )

    assert result.action == "execute"
    assert result.reason == "v1_complete_pending_signal"
    assert result.extracted["order_type"] == "pending"
    assert result.extracted["entry_plan"] == [
        {"entry_index": 1, "order_type": "sell_limit", "price": "4541"}
    ]
    assert result.extracted["entry_low"] == "4541"
    assert result.extracted["entry_high"] == "4545"
