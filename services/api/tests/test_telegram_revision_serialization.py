from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from app.telegram_listener_day28 import Day28TelegramListenerManager
from app.telegram_revision_serialization import _run_message_serialized, _stripe_index


def test_same_provider_message_always_uses_same_serialization_stripe() -> None:
    source = uuid4()
    assert _stripe_index(source, 6560) == _stripe_index(source, 6560)


def test_same_message_work_cannot_overtake_previous_revision() -> None:
    holder = SimpleNamespace()
    source = uuid4()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    order: list[str] = []

    def first_work() -> None:
        order.append("first-start")
        first_entered.set()
        assert release_first.wait(timeout=2)
        order.append("first-end")

    def second_work() -> None:
        order.append("second-start")
        second_entered.set()

    first = threading.Thread(
        target=lambda: _run_message_serialized(holder, source, 6560, first_work)
    )
    second = threading.Thread(
        target=lambda: _run_message_serialized(holder, source, 6560, second_work)
    )

    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert order == ["first-start", "first-end", "second-start"]


def test_different_messages_can_continue_in_parallel() -> None:
    holder = SimpleNamespace()
    source = uuid4()
    # Select two ids that definitely hash to different stripes for this process.
    first_id = 1
    second_id = next(
        candidate
        for candidate in range(2, 1000)
        if _stripe_index(source, candidate) != _stripe_index(source, first_id)
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first_work() -> None:
        first_entered.set()
        assert release_first.wait(timeout=2)

    def second_work() -> None:
        second_entered.set()

    first = threading.Thread(
        target=lambda: _run_message_serialized(holder, source, first_id, first_work)
    )
    second = threading.Thread(
        target=lambda: _run_message_serialized(holder, source, second_id, second_work)
    )

    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    assert second_entered.wait(timeout=1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)


def test_production_listener_methods_are_wrapped() -> None:
    assert getattr(
        Day28TelegramListenerManager._persist_message,
        "_telegram_revision_serialized",
        False,
    )
    assert getattr(
        Day28TelegramListenerManager._persist_edit,
        "_telegram_revision_serialized",
        False,
    )
