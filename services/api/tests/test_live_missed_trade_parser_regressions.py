from hashlib import sha256

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy


def _decision(*, side: str, entry_low, entry_high, stop_loss: str, take_profits: list[str], order_type: str = "market", symbol: str = "XAUUSD") -> AiMessageDecision:
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=0.99,
        reason="live_regression_fixture",
        extracted={
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop_loss,
            "take_profits": take_profits,
            "double_lot": False,
            "update_type": None,
            "update_target": None,
            "update_value": None,
            "provider_claimed_pips": None,
        },
        model="fixture",
        response_id=None,
        latency_ms=0,
        source="fixture",
        raw_text_sha256=sha256(b"fixture").hexdigest(),
    )


def test_pipxpert_xau_slash_usd_trade_3161_executes() -> None:
    raw = (
        "📣XAU/USD📣\n\nDirection: BUY\n\nEntry Price:  4460.00\n\n"
        "TP1       4465.00\nTP2       4489.00\nTP3       4521.00\n\nSL        4439.00"
    )
    result = apply_v1_message_policy(
        _decision(
            side="BUY",
            entry_low="4460.00",
            entry_high="4460.00",
            stop_loss="4439.00",
            take_profits=["4465.00", "4489.00", "4521.00"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.extracted["symbol"] == "XAUUSD"
    assert result.reason == "v1_complete_exact_signal"


def test_fxtradingvision_buy_stop_82721_recovers_literal_single_entry() -> None:
    raw = (
        "NEW TRADE IDEA\n\nXAUUSD BUY STOP 4503\n\n"
        "TP 1 4507\nTP 2 4508\nTP 3 4530\n\nSL @ 4475"
    )
    result = apply_v1_message_policy(
        _decision(
            side="BUY",
            order_type="pending",
            entry_low="4503",
            entry_high=None,
            stop_loss="4475",
            take_profits=["4507", "4508", "4530"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_pending_signal"
    assert result.extracted["entry_low"] == "4503"
    assert result.extracted["entry_high"] == "4503"
    assert result.extracted["entry_plan"] == [
        {"entry_index": 1, "order_type": "buy_stop", "price": "4503"}
    ]


def test_gold_long_trade_direction_is_explicit_buy_evidence() -> None:
    raw = (
        "#GOLD LONG FROM SUPPORT🟢\n\n📈GOLD SIGNAL\n\n"
        "✔️Trade Direction: long\n✔️Entry Level: 4397.03\n"
        "✔️Target Level: 4422.75\n✔️Stop Loss: 4379.77"
    )
    result = apply_v1_message_policy(
        _decision(
            side="BUY",
            symbol="GOLD",
            entry_low="4397.03",
            entry_high="4397.03",
            stop_loss="4379.77",
            take_profits=["4422.75"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.extracted["side"] == "BUY"
    assert result.extracted["symbol"] == "XAUUSD"


def test_gold_short_trade_direction_is_explicit_sell_evidence() -> None:
    raw = (
        "#GOLD BEARISH BIAS RIGHT NOW| SHORT🔴\n\n📉GOLD SIGNAL\n\n"
        "✔️Trade Direction: short\n✔️Entry Level: 4353.40\n"
        "✔️Target Level: 4302.61\n✔️Stop Loss: 4387.33"
    )
    result = apply_v1_message_policy(
        _decision(
            side="SELL",
            entry_low="4353.40",
            entry_high="4353.40",
            stop_loss="4387.33",
            take_profits=["4302.61"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.extracted["side"] == "SELL"
