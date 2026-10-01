from __future__ import annotations

from app.production_ai_pipeline import ProductionAiMessagePipeline
from app.v1_message_policy import apply_v1_message_policy


_MISSED_FX_TRADE = """*NEW TRADE IDEA*

XAUUSD SELL 4183

TP 1 4179
TP 2 4178
TP 3 4160

SL @ 4200

Trade accordingly and only trade with money you can afford to LOSE!

DOUBLE LOTSIZE
"""


def test_complete_fxtradingvision_trade_bypasses_paid_ai_dependency() -> None:
    decision = ProductionAiMessagePipeline._deterministic_complete_gold_trade(
        _MISSED_FX_TRADE,
        source_status="testing",
    )

    assert decision is not None
    assert decision.decision == "new_trade"
    assert decision.action == "execute"
    assert decision.source == "deterministic_no_ai"
    assert decision.extracted["symbol"] == "XAUUSD"
    assert decision.extracted["side"] == "SELL"
    assert decision.extracted["entry_low"] == "4183"
    assert decision.extracted["entry_high"] == "4183"
    assert decision.extracted["stop_loss"] == "4200"
    assert decision.extracted["take_profits"] == ["4179", "4178", "4160"]
    # Promotional provider wording must never override the locked product risk policy.
    assert decision.extracted["double_lot"] is False

    validated = apply_v1_message_policy(decision, raw_text=_MISSED_FX_TRADE)
    assert validated.action == "execute"


def test_fxtradingvision_wrapper_does_not_make_incomplete_trade_executable() -> None:
    incomplete = _MISSED_FX_TRADE.replace("SL @ 4200\n\n", "")

    decision = ProductionAiMessagePipeline._deterministic_complete_gold_trade(
        incomplete,
        source_status="testing",
    )

    assert decision is None
