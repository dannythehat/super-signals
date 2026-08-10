from app.signal_lifecycle import render_provider_update


def test_target_hit_and_break_even_render_as_one_clean_update() -> None:
    result = render_provider_update(
        "TP1 HIT - move SL to breakeven from PRIVATE PROVIDER",
        ["target_hit", "move_stop", "break_even"],
    )
    assert result is not None
    assert result.event_type == "take_profit_hit"
    assert result.text == (
        "TRADE UPDATE\n"
        "TP1 reached.\n"
        "Stop loss moved to entry on the remaining positions."
    )
    assert "PRIVATE PROVIDER" not in result.text


def test_numeric_stop_change_uses_only_supported_value() -> None:
    result = render_provider_update(
        "MOVE SL TO 4180 - provider commentary must not leak",
        ["move_stop"],
    )
    assert result is not None
    assert result.event_type == "stop_change"
    assert result.text == "TRADE UPDATE\nStop loss changed to 4180."
    assert "provider commentary" not in result.text


def test_partial_close_percentage_is_normalised() -> None:
    result = render_provider_update(
        "CLOSE 50% NOW secret source wording",
        ["close_trade"],
    )
    assert result is not None
    assert result.event_type == "partial_close"
    assert result.text == "TRADE UPDATE\nPartial close of 50% instructed."
    assert "secret source wording" not in result.text


def test_plain_close_is_instruction_not_fabricated_final_execution() -> None:
    result = render_provider_update("CLOSE NOW", ["close_trade"])
    assert result is not None
    assert result.event_type == "close_instruction"
    assert result.text == "TRADE UPDATE\nClose instruction received."


def test_stop_hit_is_clean_and_provider_free() -> None:
    result = render_provider_update(
        "SL HIT - message from provider X",
        ["stop_hit"],
    )
    assert result is not None
    assert result.event_type == "stop_loss_hit"
    assert result.text == "TRADE UPDATE\nStop loss reached."
    assert "provider X" not in result.text


def test_unsupported_update_does_not_publish_arbitrary_text() -> None:
    assert render_provider_update("random update text", ["reply_management"]) is None
