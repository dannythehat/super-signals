from app.day27_management_policy import extract_day27_management_actions


CLOSE_ALL = {"type": "close", "target": "all", "value": None}
CLOSE_TP1 = {"type": "close", "target": "TP1", "value": None}
MOVE_BE = {"type": "move_to_break_even", "target": "all", "value": None}


def test_gtmo_set_breakeven_now_is_an_explicit_instruction() -> None:
    """Exact GTMO message 60851 from the 14 Aug paper trade."""
    result = extract_day27_management_actions(
        "TP1 check set breakeven nowww!!! 🚀🚀🚀"
    )
    assert result.reason == "day27_explicit_management"
    assert result.actions == (MOVE_BE,)


def test_other_observed_set_breakeven_dialects_are_supported() -> None:
    for text in (
        "TP3 checkkk!!! set breakeven now for zero risk!!!",
        "4375 - Set Break Even⭐",
        "TP2 checkkk set fully breakeven now",
    ):
        result = extract_day27_management_actions(text)
        assert MOVE_BE in result.actions, text


def test_observed_immediate_close_dialects_close_the_mapped_trade() -> None:
    for text in (
        "Close out",
        "Close the sell now",
        "CLOSE our trade now",
        "Let's close this set up here and we wait and looking for better entry",
        "Im closing out now at a small loss",
    ):
        result = extract_day27_management_actions(text)
        assert CLOSE_ALL in result.actions, text


def test_sureshot_close_partial_closes_only_the_first_tp_leg() -> None:
    result = extract_day27_management_actions(
        "XAUUSD CLOSE PARTIAL 115+ PIPS PROFIT ✅✅✅"
    )
    assert CLOSE_TP1 in result.actions
    assert CLOSE_ALL not in result.actions


def test_gtmo_future_tp1_partial_instruction_does_not_close_early() -> None:
    """60846 says to take partials AT TP1. It is not a close-now command.

    TP1 is already broker-owned as a numeric target, so this wording must not close a
    mapped leg before price actually reaches TP1.
    """
    result = extract_day27_management_actions(
        "All entries in Profits now! 🤑\n\n"
        "At TP1 take partially profits, start with closing worst entries first always🚀"
    )
    assert CLOSE_TP1 not in result.actions
    assert CLOSE_ALL not in result.actions


def test_close_or_breakeven_choice_uses_protective_branch_not_forced_exit() -> None:
    """Observed UKS/TGC style offers a choice. Existing owner rule chooses BE rather
    than inferring a full close and cutting a winner short."""
    for text in (
        "Let's CLOSE our trade now, or set breakeven if you plan to hold on‼️",
        "Close this out in blue or BE",
        "Close out overall in blue or BE - dont like price action",
    ):
        result = extract_day27_management_actions(text)
        assert MOVE_BE in result.actions, text
        assert CLOSE_ALL not in result.actions, text


def test_past_tense_close_results_are_still_not_broker_commands() -> None:
    """Do not turn provider result reporting into a second close instruction."""
    for text in (
        "All positions closed!",
        "All other entries are now closed",
        "Ive closed out 67/68 and 66 is at BE",
    ):
        result = extract_day27_management_actions(text)
        assert not any(action["type"] == "close" for action in result.actions), text
