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


def test_optional_protective_wording_does_not_mutate_broker_position() -> None:
    """Optional provider wording is not authority to move a stop immediately.

    The owner's automatic rule protects the runner only after TP1 and TP2 are
    broker-confirmed; a provider's "if you want" must not bypass that milestone.
    """
    result = extract_day27_management_actions(
        "Trade in +40 pips profit. Make the trade risk-free if you want"
    )
    assert result.actions == ()
    assert result.reason == "optional_management_instruction"


def test_choice_between_banking_and_be_takes_the_protective_branch() -> None:
    """A literal close-or-BE choice retains the established protective branch."""
    result = extract_day27_management_actions("Bank the blue or go to BE")
    assert result.actions == ({"type": "move_to_break_even", "target": "all", "value": None},)
    assert result.reason == "optional_protective_resolved_to_breakeven"


def test_optional_entry_wording_is_still_never_executable() -> None:
    """The caution rule covers protecting an open trade only. Optional wording about
    entering or adding must stay completely non-executable -- taking the "cautious"
    branch of an entry choice would mean opening a position nobody instructed."""
    for text in (
        "Add a second entry at 4380 if you want",
        "Consider entering another buy here",
        "Scale in at 4375, up to you",
    ):
        result = extract_day27_management_actions(text)
        assert result.actions == (), text
        assert result.reason == "optional_management_instruction", text


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


def test_exit_wording_without_the_word_close_still_closes() -> None:
    """Providers say "get out" as often as "close all". Both mean the same thing."""
    for text in ("Exit now team", "Get out of the trade", "Go flat", "Close it"):
        result = extract_day27_management_actions(text)
        assert {"type": "close", "target": "all", "value": None} in result.actions, text


def test_protective_phrasings_map_to_breakeven() -> None:
    """Wording that means "protect what we have" resolves to the stop at entry."""
    for text in (
        "Move stops to entry",
        "Lock in profits now",
        "Secure your profits here",
    ):
        result = extract_day27_management_actions(text)
        assert {"type": "move_to_break_even", "target": "all", "value": None} in result.actions, text


def test_explicit_numeric_stop_still_wins_over_breakeven() -> None:
    """An explicit price is a stronger instruction than a protective phrase, and the
    provider's literal number must not be replaced by a breakeven assumption."""
    result = extract_day27_management_actions("Lock in profits, move SL to 4385")
    types = {action["type"] for action in result.actions}
    assert "move_sl" in types or "set_sl" in types or any(
        action.get("value") == "4385" for action in result.actions
    ), result.actions
    assert "move_to_break_even" not in types


def test_take_partials_closes_the_first_tp_leg() -> None:
    """Owner reading, 14 Aug 2026: partial wording is written for followers holding
    one position. Super Signals holds one per TP level, so the equivalent is closing
    the nearest leg and leaving the rest running. No partial-volume close needed."""
    for text in (
        "TP1 hit, take partials",
        "Take partial profits here",
        "Close half now team",
        "Bank half and let the rest run",
        "Secure half here",
    ):
        result = extract_day27_management_actions(text)
        assert {"type": "close", "target": "TP1", "value": None} in result.actions, text


def test_take_partials_does_not_close_everything() -> None:
    """The whole point is that the remaining legs keep running."""
    result = extract_day27_management_actions("Take partials here")
    targets = {action["target"] for action in result.actions if action["type"] == "close"}
    assert "all" not in targets


def test_close_all_still_wins_over_partial_wording() -> None:
    """An explicit full exit is not downgraded into a partial."""
    result = extract_day27_management_actions("Close all now, take partials if you missed it")
    assert {"type": "close", "target": "all", "value": None} in result.actions
    assert {"type": "close", "target": "TP1", "value": None} not in result.actions


def test_partials_combined_with_breakeven_does_both() -> None:
    """"TP1 hit, take partials and move SL to BE" is two instructions, not one."""
    result = extract_day27_management_actions("TP1 hit, take partials and move SL to BE")
    assert {"type": "close", "target": "TP1", "value": None} in result.actions
    assert {"type": "move_to_break_even", "target": "all", "value": None} in result.actions


def test_future_intent_to_take_partials_is_not_an_instruction() -> None:
    """Telling you what they plan to do later is not telling you to do it now."""
    for text in (
        "At TP2 I'll close half profits & set breakeven for zero risk",
        "I will take partials when we get there",
        "We'll bank half once it moves",
    ):
        result = extract_day27_management_actions(text)
        assert {"type": "close", "target": "TP1", "value": None} not in result.actions, text


def test_explicit_stop_survives_optional_wording_in_same_message() -> None:
    """Real TIG message 447, observed live and lost.

    The explicit "Move SL to 4314" was discarded because a later sentence said
    "if you want". The Blueprint already requires combined messages such as
    "TP1 hit, move SL to 4385" to still produce the explicit action.
    """
    result = extract_day27_management_actions(
        "Trade is running +40pips from best entry\n\n"
        "Move SL to 4314\n\n"
        "Making Second entry Risk Free 📊 if you want team"
    )
    assert {"type": "edit_stop_loss", "target": "all", "value": "4314"} in result.actions
    # The explicit price must not be downgraded to a generic breakeven.
    assert not any(a["type"] == "move_to_break_even" for a in result.actions)


def test_live_tig_optional_risk_free_message_is_non_actionable() -> None:
    result = extract_day27_management_actions(
        "Trade is running 🏃 +30pips💸💸\n\nMake trade risk free if you want🤝"
    )
    assert result.actions == ()
    assert result.reason == "optional_management_instruction"


def test_optional_wording_still_cannot_close_a_trade() -> None:
    """Exiting is not something to infer from a sentence offering a choice."""
    result = extract_day27_management_actions("Close all if you want team")
    assert not any(a["type"] == "close" for a in result.actions)


def test_tdc_risk_free_with_price_sets_that_stop() -> None:
    """Real TDC dialect: "+20 / RISK FREE 4324" means move the stop to 4324."""
    for text, price in (
        ("+20\n\nRISK FREE 4324", "4324"),
        ("+30\n\nRisk free 4317", "4317"),
        ("+30\n\nRisk free 4311", "4311"),
    ):
        result = extract_day27_management_actions(text)
        assert {"type": "edit_stop_loss", "target": "all", "value": price} in result.actions, text
        assert not any(a["type"] == "move_to_break_even" for a in result.actions), text


def test_tig_book_wording_takes_partials() -> None:
    """Real TIG dialect. BOOK was missing from the partial-taking vocabulary."""
    for text in ("Book partial 🤑", "Book some profits team", "Book mores 🤑"):
        result = extract_day27_management_actions(text)
        assert {"type": "close", "target": "TP1", "value": None} in result.actions, text
