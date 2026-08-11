from app.signal_lifecycle import render_provider_update
from app.telegram_visual_identity import (
    decorate_lifecycle_post,
    decorate_root_post,
    public_signal_reference,
    source_colour_marker,
)


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


def test_testing_source_gets_visible_banner_and_anonymous_colour() -> None:
    text = decorate_root_post(
        "SUPER SIGNALS\nXAUUSD BUY",
        source_status="testing",
        source_id="source-a",
        signal_id="signal-a",
    )
    marker = source_colour_marker("source-a")
    reference = public_signal_reference("signal-a")
    assert text.startswith(
        f"✨🧪 TESTING 🧪✨\nDEMO / NOT LIVE\n\n{marker} SIGNAL #{reference}\n\n"
    )
    assert text.endswith("SUPER SIGNALS\nXAUUSD BUY")


def test_live_source_keeps_colour_but_has_no_testing_banner() -> None:
    text = decorate_root_post(
        "SUPER SIGNALS\nXAUUSD BUY",
        source_status="live",
        source_id="source-a",
        signal_id="signal-a",
    )
    marker = source_colour_marker("source-a")
    reference = public_signal_reference("signal-a")
    assert "TESTING" not in text
    assert text.startswith(f"{marker} SIGNAL #{reference}\n\n")


def test_lifecycle_keeps_same_colour_and_signal_reference() -> None:
    root = decorate_root_post(
        "SUPER SIGNALS",
        source_status="testing",
        source_id="source-a",
        signal_id="signal-a",
    )
    update = decorate_lifecycle_post(
        "TRADE UPDATE\nTP1 reached.",
        source_status="testing",
        source_id="source-a",
        signal_id="signal-a",
    )
    marker = source_colour_marker("source-a")
    reference = public_signal_reference("signal-a")
    assert f"{marker} SIGNAL #{reference}" in root
    assert f"{marker} #{reference}" in update
