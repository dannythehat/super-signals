from app.trading_management_canonical import CanonicalTradingManagementService


def test_protective_stop_runs_before_impossible_partial_close() -> None:
    event = {
        "aggregate_result": {
            "revised_instruction": {
                "management_actions": [
                    {"type": "close", "target": "partial_tp1", "value": None},
                    {"type": "move_to_break_even", "target": "all", "value": None},
                ]
            }
        }
    }

    actions = CanonicalTradingManagementService._actions(event)

    assert [action["type"] for action in actions] == [
        "move_to_break_even",
        "close",
    ]


def test_provider_order_is_stable_within_same_management_priority() -> None:
    event = {
        "aggregate_result": {
            "revised_instruction": {
                "management_actions": [
                    {"type": "close", "target": "tp1", "value": None},
                    {"type": "cancel_pending", "target": "all", "value": None},
                    {"type": "edit_stop_loss", "target": "all", "value": "4653.01"},
                    {"type": "move_to_break_even", "target": "remaining", "value": None},
                ]
            }
        }
    }

    actions = CanonicalTradingManagementService._actions(event)

    assert [action["type"] for action in actions] == [
        "edit_stop_loss",
        "move_to_break_even",
        "close",
        "cancel_pending",
    ]


def test_close_now_with_optional_hold_breakeven_executes_close_only() -> None:
    event = {
        "source_text": (
            "Let's CLOSE our trade now and set breakeven if you wish to hold"
        ),
        "aggregate_result": {
            "revised_instruction": {
                "management_actions": [
                    {"type": "close", "target": "all", "value": None},
                    {"type": "move_to_break_even", "target": "all", "value": None},
                ]
            }
        },
    }

    actions = CanonicalTradingManagementService._actions(event)

    assert actions == ({"type": "close", "target": "all", "value": None},)


def test_close_half_and_move_sl_remains_a_genuine_compound_instruction() -> None:
    event = {
        "source_text": "CLOSE HALF 54 PIPS PROFIT MOVE SL TO ENTRY",
        "aggregate_result": {
            "revised_instruction": {
                "management_actions": [
                    {"type": "close", "target": "partial_tp1", "value": None},
                    {"type": "move_to_break_even", "target": "all", "value": None},
                ]
            }
        },
    }

    actions = CanonicalTradingManagementService._actions(event)

    assert [action["type"] for action in actions] == [
        "move_to_break_even",
        "close",
    ]
