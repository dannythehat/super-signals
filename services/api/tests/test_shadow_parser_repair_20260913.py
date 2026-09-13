from app.ai_message_supervisor import AiMessageDecision
from app.production_ai_pipeline import ProductionAiMessagePipeline
from app.v1_message_policy import apply_v1_message_policy


def _decision(*, side, entry_low, entry_high, stop_loss, take_profits):
    return AiMessageDecision(
        decision="new_trade",
        action="skip",
        confidence=1.0,
        reason="test_candidate",
        extracted={
            "symbol": "XAUUSD",
            "side": side,
            "order_type": "market",
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
        model="test",
        response_id=None,
        latency_ms=0,
        source="test",
        raw_text_sha256="0" * 64,
    )


def test_structured_signal_field_labels_are_not_management_only() -> None:
    samples = [
        """#XAUUSD #GOLD SIGNAL
BUY  : 4334 - 4330
Take Profit: 4338 - 4348 - 4358
Stop Loss: 4320 or adjust according to your margin""",
        """📊XAUUSD SELL GOLD HIGH RISK
📍 Entry Zone SELL
➡️4413
💸 Take Profit
📉 TP 1 : 4404 SET SL+ KETIKA RUNING 50 PIPS
🛑SL : bisa gunakan 30/40 pips saya di area 4419""",
        """📊GOLD: Move Up Expected! Buy!
SIGNAL DETAILS
ENTER: #XAUUSD long trade
CURRENT PRICE: 4,349.841
STOP LOSS: 4,328.314
TAKE PROFIT: 4,383.133""",
    ]
    assert all(ProductionAiMessagePipeline._looks_like_structured_trade(value) for value in samples)
    assert not ProductionAiMessagePipeline._looks_like_structured_trade(
        "BOOM 💥 TP1 HIT ✅ | SECURE YOUR PROFITS / HOLD WITH BE+ OKAY"
    )


def test_grouped_gold_prices_are_normalized_only_for_policy() -> None:
    raw = "CURRENT PRICE: 4,349.841 | TP: $4,383 | SL: 4,328.314"
    normalized = ProductionAiMessagePipeline._policy_text(raw, None)
    assert "4349.841" in normalized
    assert "$4383" in normalized
    assert "4328.314" in normalized
    assert "4,349.841" in raw


def test_learn2trade_comma_zone_becomes_valid_canonical_trade() -> None:
    raw = """⚡ FREE SIGNAL — GOLD (XAU/USD) SHORT
🔴 Direction: SHORT
📍 Entry Zone: $4,353 – $4,375
🎯 TP1: $4,275
🎯 TP2: $4,185
🛑 SL: $4,415
#Gold #XAUUSD #FreeSignal"""
    decision = _decision(
        side="SELL",
        entry_low="4353",
        entry_high="4375",
        stop_loss="4415",
        take_profits=["4275", "4185"],
    )
    result = apply_v1_message_policy(
        decision,
        raw_text=ProductionAiMessagePipeline._policy_text(raw, None),
    )
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == "v1_complete_zone_signal"


def test_gold_signals_vip_grouped_exact_signal_is_valid() -> None:
    raw = """📊GOLD: Move Up Expected! Buy!
🆓SIGNAL DETAILS
ENTER: #XAUUSD long trade
CURRENT PRICE: 4,349.841
STOP LOSS: 4,328.314
TAKE PROFIT: 4,383.133
SUGGESTED RISK: 1%"""
    decision = _decision(
        side="BUY",
        entry_low="4349.841",
        entry_high="4349.841",
        stop_loss="4328.314",
        take_profits=["4383.133"],
    )
    result = apply_v1_message_policy(
        decision,
        raw_text=ProductionAiMessagePipeline._policy_text(raw, None),
    )
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == "v1_complete_exact_signal"


def test_hidden_stop_provider_remains_fail_closed() -> None:
    raw = """BUY GOLD @ 4383
TP1: 4386
TP2: 4390++
SL : PREMIUM
Manage your risk at all times!"""
    decision = _decision(
        side="BUY",
        entry_low="4383",
        entry_high="4383",
        stop_loss=None,
        take_profits=["4386", "4390"],
    )
    result = apply_v1_message_policy(decision, raw_text=raw)
    assert result.action == "skip"
    assert result.reason == "missing_sl"


def test_app_gated_signal_remains_fail_closed_without_geometry() -> None:
    raw = """🔔 🟢 NEW #XAU BUY SIGNAL 📈
🎯 New #XAU BUY setup is live!
📲 Open app to view Entry, Targets & Stop Loss."""
    decision = _decision(
        side="BUY",
        entry_low=None,
        entry_high=None,
        stop_loss=None,
        take_profits=[],
    )
    result = apply_v1_message_policy(decision, raw_text=raw)
    assert result.action == "skip"
