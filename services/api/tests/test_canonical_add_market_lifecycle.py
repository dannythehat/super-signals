from app.ai_lifecycle_bridge import AiLifecycleBridge
from app.day27_management_policy import extract_day27_management_actions


def test_exact_fx_open_extra_sells_reaches_lifecycle_bridge() -> None:
    raw = "Any risk takers here?\n\nOpen extra gold sells."
    policy = extract_day27_management_actions(raw)
    assert policy.actions == (
        {"type": "add_market", "target": "same_trade", "value": "SELL"},
    )

    event_type, rendered = AiLifecycleBridge._render(
        "add_market",
        {
            "update_type": "add_market",
            "update_target": "same_trade",
            "update_value": "SELL",
            "management_actions": [
                {"type": "add_market", "target": "same_trade", "value": "SELL"}
            ],
        },
    )
    assert event_type == "add_market"
    assert "SELL" in rendered


def test_existing_lifecycle_rendering_is_unchanged() -> None:
    event_type, rendered = AiLifecycleBridge._render(
        "move_to_break_even",
        {"update_type": "move_to_break_even", "update_target": "all"},
    )
    assert event_type == "break_even"
    assert "Stop loss moved to entry" in rendered
