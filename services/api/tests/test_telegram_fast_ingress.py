import time
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import app.telegram_fast_ingress as fast_ingress
from app.telegram_fast_ingress import (
    _SourceProcessingPool,
    _fast_persist_edit,
    _fast_persist_message,
)
from app.telegram_listener import TelegramListenerManager
from app.telegram_listener_day13 import Day13TelegramListenerManager


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


def test_saved_edit_processes_and_dispatches_exact_revision_without_repersist(monkeypatch) -> None:
    source_id = uuid4()
    captured = SimpleNamespace(
        source_id=source_id,
        telegram_message_id=6560,
        raw_text="BUY XAUUSD 4394-4390\nTP 4396\nTP 4398\nTP 4400\nTP 4404\nSL 4386",
        edited_at=object(),
    )
    processed: list[int] = []
    dispatched: list[int] = []
    fallback_calls: list[str] = []
    completed = Event()

    class Pipeline:
        def _process_revision(self, got_source_id, got_message_id, *, revision_index):
            assert got_source_id == source_id
            assert got_message_id == 6560
            processed.append(revision_index)
            return SimpleNamespace(reason="processed")

    def dispatch(*, source_id, telegram_message_id, revision_index):
        assert source_id == captured.source_id
        assert telegram_message_id == captured.telegram_message_id
        dispatched.append(revision_index)
        completed.set()

    manager = SimpleNamespace(
        _telegram_fast_ingress_pool=_SourceProcessingPool(),
        _ai_pipeline=Pipeline(),
        _dispatch_sync=dispatch,
    )

    monkeypatch.setattr(
        Day13TelegramListenerManager,
        "_persist_edit",
        lambda self, value: True,
    )
    # This is deliberately revision 1. A later revision may already exist in the DB;
    # the worker must never replace this with MAX(revision_index).
    monkeypatch.setattr(fast_ingress, "_exact_saved_revision_index", lambda self, value: 1)

    def fallback(self, value):
        fallback_calls.append("called")
        return True

    inserted = _fast_persist_edit(manager, captured, fallback)

    assert inserted is True
    assert completed.wait(timeout=1)
    assert processed == [1]
    assert dispatched == [1]
    assert fallback_calls == []
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
