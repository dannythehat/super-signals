"""Serialize one Telegram message's original/edit decision and broker dispatch stream.

Telethon can deliver rapid edits while a previous revision is still inside the AI /
canonical pipeline.  Day 28 historically re-read MAX(revision_index) after processing,
so a later edit could overtake the revision whose canonical write had just completed.
That allowed dispatch to target a decision that did not exist yet or a transient older
canonical shape.

Only events for the same source/message key are serialized.  Different provider
messages continue in parallel.
"""

from __future__ import annotations

from threading import RLock
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")
_STRIPE_COUNT = 128


def _stripe_index(source_id: object, telegram_message_id: int) -> int:
    return hash((str(source_id), int(telegram_message_id))) % _STRIPE_COUNT


def _run_message_serialized(self: Any, source_id: object, telegram_message_id: int, fn: Callable[[], _T]) -> _T:
    locks = getattr(self, "_telegram_revision_locks", None)
    if locks is None:
        # Defensive lazy setup for tests / unusual construction paths. Production
        # instances receive these locks from the wrapped __init__ below.
        locks = tuple(RLock() for _ in range(_STRIPE_COUNT))
        setattr(self, "_telegram_revision_locks", locks)
    with locks[_stripe_index(source_id, telegram_message_id)]:
        return fn()


def install_telegram_revision_serialization() -> None:
    from app.telegram_listener_day28 import Day28TelegramListenerManager

    cls = Day28TelegramListenerManager

    original_init = cls.__init__
    if not getattr(original_init, "_telegram_revision_serialized", False):
        def wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            self._telegram_revision_locks = tuple(RLock() for _ in range(_STRIPE_COUNT))

        wrapped_init._telegram_revision_serialized = True  # type: ignore[attr-defined]
        cls.__init__ = wrapped_init

    original_message = cls._persist_message
    if not getattr(original_message, "_telegram_revision_serialized", False):
        def persist_message(self: Any, captured: Any) -> bool:
            return _run_message_serialized(
                self,
                captured.source_id,
                captured.telegram_message_id,
                lambda: original_message(self, captured),
            )

        persist_message._telegram_revision_serialized = True  # type: ignore[attr-defined]
        cls._persist_message = persist_message

    original_edit = cls._persist_edit
    if not getattr(original_edit, "_telegram_revision_serialized", False):
        def persist_edit(self: Any, captured: Any) -> bool:
            return _run_message_serialized(
                self,
                captured.source_id,
                captured.telegram_message_id,
                lambda: original_edit(self, captured),
            )

        persist_edit._telegram_revision_serialized = True  # type: ignore[attr-defined]
        cls._persist_edit = persist_edit


__all__ = [
    "_run_message_serialized",
    "_stripe_index",
    "install_telegram_revision_serialization",
]
