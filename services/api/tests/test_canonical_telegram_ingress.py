from __future__ import annotations

import time
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

from app.production_listener import PRODUCTION_LISTENER_GENERATION
from app.telegram_listener import TelegramListenerManager
from app.telegram_listener_canonical import (
    CanonicalProductionTelegramListenerManager,
    _ProviderProcessingPool,
)


def test_production_listener_is_canonical_generation() -> None:
    assert PRODUCTION_LISTENER_GENERATION == "canonical-v1"


def test_canonical_listener_owns_ingress_recovery_and_exact_revision_processing() -> None:
    required = {
        "_persist_message",
        "_persist_edit",
        "_exact_saved_revision_index",
        "_process_saved_edit",
        "_recover_live_gaps",
        "_dispatch_recovered_if_required",
    }
    assert required.issubset(CanonicalProductionTelegramListenerManager.__dict__)


def test_raw_save_returns_without_waiting_for_slow_downstream(monkeypatch) -> None:
    source_id = uuid4()
    captured = SimpleNamespace(source_id=source_id, telegram_message_id=12345)
    downstream_started = Event()
    release_downstream = Event()

    manager = object.__new__(CanonicalProductionTelegramListenerManager)
    manager._telegram_processing_pool = _ProviderProcessingPool()
    manager._telegram_revision_locks = tuple(__import__("threading").RLock() for _ in range(128))

    monkeypatch.setattr(TelegramListenerManager, "_persist_message", lambda self, value: True)

    def slow(value):
        downstream_started.set()
        release_downstream.wait(timeout=2)
        return True

    monkeypatch.setattr(manager, "_process_saved_original", slow)
    started = time.monotonic()
    inserted = manager._persist_message(captured)
    elapsed = time.monotonic() - started

    assert inserted is True
    assert elapsed < 0.20
    assert downstream_started.wait(timeout=1)
    release_downstream.set()
    manager._telegram_processing_pool.shutdown()


def test_canonical_dispatch_has_no_cross_provider_global_lock() -> None:
    source = (
        __import__("pathlib").Path("services/api/app/telegram_listener_canonical.py")
        .read_text()
    )
    assert "self._dispatch_lock" not in source
    assert "one slow MetaAPI call would otherwise freeze" in source


def test_same_provider_fifo_and_different_providers_concurrent() -> None:
    pool = _ProviderProcessingPool()
    source = uuid4()
    first_started = Event()
    release_first = Event()
    same_source_second_started = Event()
    other_source_started = Event()
    order: list[str] = []

    def first() -> None:
        order.append("first-start")
        first_started.set()
        release_first.wait(timeout=2)
        order.append("first-end")

    def same_source_second() -> None:
        order.append("second-start")
        same_source_second_started.set()

    def other_source() -> None:
        other_source_started.set()

    first_future = pool.submit(source, first)
    second_future = pool.submit(source, same_source_second)
    other_future = pool.submit(uuid4(), other_source)

    assert first_started.wait(timeout=1)
    assert other_source_started.wait(timeout=1)
    time.sleep(0.05)
    assert not same_source_second_started.is_set()

    release_first.set()
    first_future.result(timeout=1)
    second_future.result(timeout=1)
    other_future.result(timeout=1)
    assert order == ["first-start", "first-end", "second-start"]
    pool.shutdown()


def test_same_message_revision_lock_serializes_work() -> None:
    source = uuid4()
    manager = object.__new__(CanonicalProductionTelegramListenerManager)
    manager._telegram_revision_locks = tuple(__import__("threading").RLock() for _ in range(128))
    lock_a = manager._revision_lock(source, 6560)
    lock_b = manager._revision_lock(source, 6560)
    assert lock_a is lock_b

    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    order: list[str] = []

    def first() -> None:
        with manager._revision_lock(source, 6560):
            order.append("first-start")
            first_entered.set()
            release_first.wait(timeout=2)
            order.append("first-end")

    def second() -> None:
        with manager._revision_lock(source, 6560):
            order.append("second-start")
            second_entered.set()

    first_thread = Thread(target=first)
    second_thread = Thread(target=second)
    first_thread.start()
    assert first_entered.wait(timeout=1)
    second_thread.start()
    time.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert order == ["first-start", "first-end", "second-start"]


def test_deleted_telegram_patch_modules_are_not_package_startup_dependencies() -> None:
    import app

    package_source = app.__loader__.get_source("app") if app.__loader__ is not None else ""
    for deleted in (
        "telegram_fast_ingress",
        "telegram_revision_serialization",
        "aug18_trade_capture_overrides",
    ):
        assert deleted not in (package_source or "")
