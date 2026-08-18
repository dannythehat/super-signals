from types import SimpleNamespace

from app.day27_management_policy import extract_day27_management_actions
from app.v1_message_policy import apply_v1_message_policy


def _decision(text: str):
    return SimpleNamespace(
        decision="trade_update",
        action="ignore",
        reason="",
        extracted={
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_low": None,
            "entry_high": None,
            "stop_loss": None,
            "take_profits": [],
        },
    )


def test_fxt_move_gold_stoploss_is_explicit_numeric_management() -> None:
    raw = "Move gold stoploss to 4375.\n\nI will give you a counter trade soon.\n\nBe ready."
    result = extract_day27_management_actions(raw)
    assert {"type": "edit_stop_loss", "target": "all", "value": "4375"} in result.actions


def test_fxt_result_text_does_not_hide_move_sl_back_to_entry() -> None:
    raw = (
        "TP 1 & 2 are BOTH hit ✅\n\n"
        "+ 90 pips profit secured as we used double lotsize 🔥🔥🔥\n\n"
        "Move your SL back to entry."
    )
    result = extract_day27_management_actions(raw)
    assert {"type": "move_to_break_even", "target": "all", "value": None} in result.actions


def test_v1_fxt_result_plus_management_remains_actionable() -> None:
    raw = (
        "TP 1 & 2 are BOTH hit ✅\n\n"
        "+ 90 pips profit secured as we used double lotsize 🔥🔥🔥\n\n"
        "Move your SL back to entry."
    )
    decision = _decision(raw)
    allowed = apply_v1_message_policy(decision, raw_text=raw)
    assert allowed.action == "apply_update"
    assert allowed.extracted["management_actions"] == [
        {"type": "move_to_break_even", "target": "all", "value": None}
    ]
