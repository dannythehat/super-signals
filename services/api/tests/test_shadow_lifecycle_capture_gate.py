from __future__ import annotations

import inspect

from app.telegram_listener import LISTENABLE_SOURCE_STATES
from app.telegram_listener_day13 import Day13TelegramListenerManager


def test_shadow_is_a_listenable_source_state() -> None:
    assert LISTENABLE_SOURCE_STATES == {"testing", "shadow", "live"}


def test_edit_persistence_uses_shared_listenable_states() -> None:
    source = inspect.getsource(Day13TelegramListenerManager._persist_edit)
    assert "Source.status.in_(LISTENABLE_SOURCE_STATES)" in source
    assert "{\"testing\", \"live\"}" not in source


def test_deletion_persistence_uses_shared_listenable_states() -> None:
    source = inspect.getsource(Day13TelegramListenerManager._persist_deletion)
    assert "Source.status.in_(LISTENABLE_SOURCE_STATES)" in source
    assert "{\"testing\", \"live\"}" not in source


def test_chatless_deletion_resolution_uses_shared_listenable_states() -> None:
    source = inspect.getsource(Day13TelegramListenerManager._resolve_chatless_deletions)
    assert "Source.status.in_(LISTENABLE_SOURCE_STATES)" in source
    assert "{\"testing\", \"live\"}" not in source
