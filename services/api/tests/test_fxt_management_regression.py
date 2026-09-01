from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy

import app.day27_management_policy as day27


def _decision() -> AiMessageDecision:
    return AiMessageDecision(
        decision="trade_update",
        action="ignore",
        confidence=1.0,
        reason="provider_result_only",
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


def test_fxt_plural_gold_stoplosses_are_explicit_numeric_management() -> None:
    raw = "Move all gold stoplosses to 4405 to be safe from wicks."
    result = day27.extract_day27_management_actions(raw)
    assert {"type": "edit_stop_loss", "target": "all", "value": "4405"} in result.actions


def test_fxt_result_text_closes_hit_tps_before_move_to_entry() -> None:
    raw = (
        "TP 1 & 2 are BOTH hit ✅\n\n"
        "+ 90 pips profit secured as we used double lotsize 🔥🔥🔥\n\n"
        "Move your SL back to entry."
    )
    result = day27.extract_day27_management_actions(raw)
    assert result.actions == (
        {"type": "close", "target": "TP1", "value": None},
        {"type": "close", "target": "TP2", "value": None},
        {"type": "move_to_break_even", "target": "all", "value": None},
    )


def test_fxt_close_with_small_loss_is_explicit_close_all() -> None:
    raw = "Close with small loss."
    result = day27.extract_day27_management_actions(raw)
    assert result.actions == (
        {"type": "close", "target": "all", "value": None},
    )
    allowed = apply_v1_message_policy(_decision(), raw_text=raw)
    assert allowed.decision == "trade_update"
    assert allowed.action == "apply_update"
    assert allowed.extracted["management_actions"] == [
        {"type": "close", "target": "all", "value": None},
    ]


def test_fxt_bare_sl_back_to_entry_is_explicit_breakeven() -> None:
    raw = "TP 1 is hit! 👏\n\n+ 40 pips profit secured.\n\nSL back to entry."
    result = day27.extract_day27_management_actions(raw)
    assert result.actions == (
        {"type": "close", "target": "TP1", "value": None},
        {"type": "move_to_break_even", "target": "all", "value": None},
    )
    allowed = apply_v1_message_policy(_decision(), raw_text=raw)
    assert allowed.decision == "trade_update"
    assert allowed.action == "apply_update"


def test_v1_fxt_result_plus_management_overrides_ai_ignore() -> None:
    raw = (
        "TP 1 & 2 are BOTH hit ✅\n\n"
        "+ 90 pips profit secured as we used double lotsize 🔥🔥🔥\n\n"
        "Move your SL back to entry."
    )
    allowed = apply_v1_message_policy(_decision(), raw_text=raw)
    assert allowed.decision == "trade_update"
    assert allowed.action == "apply_update"
    assert allowed.extracted["management_actions"] == [
        {"type": "close", "target": "TP1", "value": None},
        {"type": "close", "target": "TP2", "value": None},
        {"type": "move_to_break_even", "target": "all", "value": None},
    ]


def test_standalone_tp_hit_remains_result_only() -> None:
    raw = "TP2 HIT ✅"
    allowed = apply_v1_message_policy(_decision(), raw_text=raw)
    assert allowed.action != "apply_update"


def test_gtmo_set_breakeven_nowww_remains_actionable() -> None:
    raw = "TP1 check set breakeven nowww!!!"
    allowed = apply_v1_message_policy(_decision(), raw_text=raw)
    assert allowed.action == "apply_update"
    assert {
        "type": "move_to_break_even",
        "target": "all",
        "value": None,
    } in allowed.extracted["management_actions"]
