from app.ai_message_supervisor import AiMessageDecision
from app.literal_management_overrides import install_literal_management_overrides
from app.v1_message_policy import apply_v1_message_policy

install_literal_management_overrides()

import app.day27_management_policy as day27


def _decision() -> AiMessageDecision:
    return AiMessageDecision(
        decision="trade_update",
        action="ignore",
        confidence=1.0,
        reason="",
        extracted={
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_low": None,
            "entry_high": None,
            "stop_loss": None,
            "take_profits": [],
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
        raw_text_sha256="x",
    )


def test_fxt_move_gold_stoploss_is_explicit_numeric_management() -> None:
    raw = "Move gold stoploss to 4375.\n\nI will give you a counter trade soon.\n\nBe ready."
    result = day27.extract_day27_management_actions(raw)
    assert {"type": "edit_stop_loss", "target": "all", "value": "4375"} in result.actions


def test_fxt_result_text_does_not_hide_move_sl_back_to_entry() -> None:
    raw = (
        "TP 1 & 2 are BOTH hit ✅\n\n"
        "+ 90 pips profit secured as we used double lotsize 🔥🔥🔥\n\n"
        "Move your SL back to entry."
    )
    result = day27.extract_day27_management_actions(raw)
    assert {"type": "move_to_break_even", "target": "all", "value": None} in result.actions


def test_v1_fxt_result_plus_management_remains_actionable() -> None:
    raw = (
        "TP 1 & 2 are BOTH hit ✅\n\n"
        "+ 90 pips profit secured as we used double lotsize 🔥🔥🔥\n\n"
        "Move your SL back to entry."
    )
    allowed = apply_v1_message_policy(_decision(), raw_text=raw)
    assert allowed.action == "apply_update"
    assert allowed.extracted["management_actions"] == [
        {"type": "move_to_break_even", "target": "all", "value": None}
    ]
