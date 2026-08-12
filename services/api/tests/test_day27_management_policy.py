from app.day27_management_policy import extract_day27_management_actions


def _actions(text: str):
    return extract_day27_management_actions(text).actions


def test_tdc_numeric_stop_is_explicit() -> None:
    result = extract_day27_management_actions("For those who may still be holding use 4357 as an SL")
    assert result.reason == "day27_explicit_management"
    assert result.actions == ({"type": "edit_stop_loss", "target": "all", "value": "4357"},)


def test_tig_move_stop_and_close_first_entry_preserves_order() -> None:
    actions = _actions("Move SL to 4391✅\n\nClose first entry at BE✅")
    assert actions[0] == {"type": "close", "target": "entry_1", "value": None}
    assert actions[1] == {"type": "edit_stop_loss", "target": "all", "value": "4391"}


def test_close_specific_tp() -> None:
    assert _actions("Close TP1 now") == ({"type": "close", "target": "TP1", "value": None},)


def test_close_all() -> None:
    assert _actions("Close all!!") == ({"type": "close", "target": "all", "value": None},)


def test_out_at_entry_on_rest_is_close_all_remaining() -> None:
    assert _actions("OUT AT ENTRY ON THE REST OF MY POSITION") == (
        {"type": "close", "target": "all", "value": None},
    )


def test_explicit_break_even() -> None:
    assert _actions("I will make my trade risk free now") == (
        {"type": "move_to_break_even", "target": "all", "value": None},
    )


def test_breakeven_result_is_not_instruction() -> None:
    result = extract_day27_management_actions("Im at BE")
    assert result.actions == ()
    assert result.reason == "provider_result_only"


def test_optional_if_you_want_is_never_executable() -> None:
    result = extract_day27_management_actions("Trade in +40 pips profit. Make the trade risk-free if you want")
    assert result.actions == ()
    assert result.reason == "optional_management_instruction"


def test_choice_bank_blue_or_be_is_never_executable() -> None:
    result = extract_day27_management_actions("Bank the blue or go to BE")
    assert result.actions == ()
    assert result.reason == "optional_management_instruction"


def test_future_conditional_wording_not_promoted_by_helper() -> None:
    # This is deliberately not an immediate BE syntax in the mechanical helper.
    result = extract_day27_management_actions("At TP2 I'll close half profits & set breakeven for zero risk")
    assert result.actions == ()


def test_cancel_reply_wording() -> None:
    assert _actions("Cancel this") == ({"type": "cancel_pending", "target": "all", "value": None},)


def test_numeric_tp_change_requires_target_number() -> None:
    assert _actions("Move TP2 to 4405") == (
        {"type": "edit_take_profit", "target": "TP2", "value": "4405"},
    )


def test_combined_result_and_explicit_stop_still_extracts_stop() -> None:
    actions = _actions("TP1 HIT, +60 PIPS. Take partials and move stoploss to 4385 making the trade risk-free")
    assert {"type": "edit_stop_loss", "target": "all", "value": "4385"} in actions
