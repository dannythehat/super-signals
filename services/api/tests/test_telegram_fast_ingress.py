import time
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from app.telegram_fast_ingress import _SourceProcessingPool, _fast_persist_message
from app.telegram_listener import TelegramListenerManager


def test_live_ingress_returns_after_raw_save_without_waiting_for_ai_or_broker(monkeypatch) -> None:
    manager = SimpleNamespace(_telegram_fast_ingress_pool=_SourceProcessingPool())
    captured = SimpleNamespace(source_id=uuid4(), telegram_message_id=12345)
    downstream_started = Event()
    release_downstream = Event()

    monkeypatch.setattr(
        TelegramListenerManager,
        "_persist_message",
        lambda self, value: True,
    )

    def slow_downstream(self, value):
        downstream_started.set()
        release_downstream.wait(timeout=2)
        return True

    started = time.monotonic()
    inserted = _fast_persist_message(manager, captured, slow_downstream)
    elapsed = time.monotonic() - started

    assert inserted is True
    assert elapsed < 0.20
    assert downstream_started.wait(timeout=1)
    release_downstream.set()
    manager._telegram_fast_ingress_pool.shutdown()


def test_same_provider_processing_remains_fifo() -> None:
    pool = _SourceProcessingPool()
    source_id = uuid4()
    first_started = Event()
    release_first = Event()
    second_started = Event()
    order: list[str] = []

    def first() -> None:
        order.append("first-start")
        first_started.set()
        release_first.wait(timeout=2)
        order.append("first-end")

    def second() -> None:
        order.append("second-start")
        second_started.set()

    first_future = pool.submit(source_id, first)
    second_future = pool.submit(source_id, second)

    assert first_started.wait(timeout=1)
    time.sleep(0.05)
    assert not second_started.is_set()

    release_first.set()
    first_future.result(timeout=1)
    second_future.result(timeout=1)

    assert order == ["first-start", "first-end", "second-start"]
    pool.shutdown()


def test_different_providers_do_not_share_one_processing_lane() -> None:
    pool = _SourceProcessingPool()
    first_release = Event()
    second_started = Event()

    def blocked_first() -> None:
        first_release.wait(timeout=2)

    def second() -> None:
        second_started.set()

    first_future = pool.submit(uuid4(), blocked_first)
    second_future = pool.submit(uuid4(), second)

    assert second_started.wait(timeout=1)
    first_release.set()
    first_future.result(timeout=1)
    second_future.result(timeout=1)
    pool.shutdown()
