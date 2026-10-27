"""Exact joined-Telegram-channel matching for requested Wolves Trading onboarding."""
from types import SimpleNamespace

from app.onboard_wolves_trading import _is_target_dialog


def _dialog(username: str | None, *, channel: bool = True):
    return SimpleNamespace(
        entity=SimpleNamespace(username=username),
        is_channel=channel,
    )


def test_wolves_username_matches_exactly_case_insensitive():
    assert _is_target_dialog(_dialog("WolvesTrades"))
    assert _is_target_dialog(_dialog("wolvestrades"))


def test_similarly_named_sources_are_not_added():
    assert not _is_target_dialog(_dialog("wolvestradesforex"))
    assert not _is_target_dialog(_dialog("wolvesinfo"))
    assert not _is_target_dialog(_dialog("otherchannel"))
    assert not _is_target_dialog(_dialog(None))
    assert not _is_target_dialog(_dialog("WolvesTrades", channel=False))
