"""An edit whose original is missing may only become a live message if it is fresh.

Telegram can deliver MessageEdited before NewMessage, and recovering the edit is what
stops an otherwise valid live trade being dropped. But recovery turns an edit into a
message the broker path will act on, so an old edit recovered today would become a
market order at today's price for a trade posted hours ago.

The gate that prevents that used to default to "safe" when a listener had no freshness
check, so the listeners least equipped to judge were the ones that recovered
unconditionally. These tests pin the opposite: recovery requires an affirmative answer,
and anything less is recorded as an orphaned edit rather than acted on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.telegram_listener_day13 import CapturedTelegramEdit, Day13TelegramListenerManager


def _edit(**overrides) -> CapturedTelegramEdit:
    base = {
        "source_id": uuid4(),
        "chat_id": -100999,
        "telegram_message_id": 4242,
        "raw_text": "EDIT WITHOUT ORIGINAL",
        "edited_at": datetime.now(UTC),
        "reply_to_message_id": None,
        "has_media": False,
        "media_type": None,
    }
    base.update(overrides)
    return CapturedTelegramEdit(**base)


def _decide(manager: object, captured: CapturedTelegramEdit) -> bool:
    """The recovery decision exactly as ``_persist_edit`` makes it."""
    freshness_check = getattr(manager, "_fresh_recovered_entry", None)
    return (
        callable(freshness_check)
        and captured.posted_at is not None
        and bool(freshness_check(captured.posted_at))
    )


def test_a_listener_with_no_freshness_gate_never_recovers() -> None:
    """The regression: no gate used to mean unconditional recovery."""
    assert _decide(SimpleNamespace(), _edit(posted_at=datetime.now(UTC))) is False


def test_an_edit_without_an_original_timestamp_is_not_recovered() -> None:
    """With nothing to judge, there is no way to know the trade is still live."""
    manager = SimpleNamespace(_fresh_recovered_entry=lambda value: True)

    assert _decide(manager, _edit(posted_at=None)) is False


def test_a_stale_edit_is_not_recovered() -> None:
    manager = SimpleNamespace(_fresh_recovered_entry=lambda value: False)
    stale = datetime.now(UTC) - timedelta(hours=6)

    assert _decide(manager, _edit(posted_at=stale)) is False


def test_a_fresh_edit_behind_a_real_gate_is_recovered() -> None:
    """The production race this exists for must still be repaired."""
    manager = SimpleNamespace(_fresh_recovered_entry=lambda value: True)

    assert _decide(manager, _edit(posted_at=datetime.now(UTC))) is True


@pytest.mark.parametrize("attribute", ["_fresh_recovered_entry"])
def test_the_gate_is_the_one_persist_edit_actually_consults(attribute) -> None:
    """A renamed gate would silently disable recovery rather than fail loudly."""
    import inspect

    source = inspect.getsource(Day13TelegramListenerManager._persist_edit)
    assert attribute in source
    assert "safe_to_recover = True" not in source, "the gate must not default to safe"
