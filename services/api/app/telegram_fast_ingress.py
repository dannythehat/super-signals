"""Fast live Telegram ingress for production paper testing.

The live event handler must do one thing first: durably persist the provider message or
edit. Classification, AI supervision, canonical signal creation and broker dispatch then
run on an ordered per-source worker. This prevents slow AI/broker work for one provider
message from blocking Telethon from receiving the next update.

Recovery/catch-up paths deliberately call the explicit persistence methods and retain
their own freshness controls. This module only changes the live dynamic listener
persistence hooks used by the push event handlers.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any, Callable

logger = logging.getLogger(__name__)


class _SourceProcessingPool:
    """One FIFO worker per provider source; different providers remain concurrent."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._closed = False

    def submit(
        self,
        source_id: object,
        fn: Callable[..., Any],
        *args: Any,
    ) -> Future[Any]:
        key = str(source_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("telegram_fast_ingress_closed")
            executor = self._executors.get(key)
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix=f"ss-provider-{key[:8]}",
                )
                self._executors[key] = executor
        return executor.submit(fn, *args)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            executors = tuple(self._executors.values())
            self._executors.clear()
        for executor in executors:
            # Already-accepted work is allowed to finish. New work is rejected.
            executor.shutdown(wait=False, cancel_futures=False)


def _pool_for(manager: Any) -> _SourceProcessingPool:
    pool = getattr(manager, "_telegram_fast_ingress_pool", None)
    if pool is None:
        pool = _SourceProcessingPool()
        setattr(manager, "_telegram_fast_ingress_pool", pool)
    return pool


def _report_future(
    future: Future[Any],
    *,
    source_id: object,
    telegram_message_id: int,
    kind: str,
) -> None:
    try:
        future.result()
    except Exception:
        logger.exception(
            "Telegram post-persist processing failed kind=%s source=%s message=%s",
            kind,
            source_id,
            telegram_message_id,
        )


def _submit_processing(
    manager: Any,
    captured: Any,
    downstream: Callable[[Any, Any], Any],
    *,
    kind: str,
) -> None:
    future = _pool_for(manager).submit(
        captured.source_id,
        downstream,
        manager,
        captured,
    )
    future.add_done_callback(
        lambda done: _report_future(
            done,
            source_id=captured.source_id,
            telegram_message_id=int(captured.telegram_message_id),
            kind=kind,
        )
    )


def _fast_persist_message(
    manager: Any,
    captured: Any,
    downstream: Callable[[Any, Any], Any],
) -> bool:
    from app.telegram_listener import TelegramListenerManager

    # Call the raw-ingestion implementation directly so no classifier/AI/broker code
    # runs before the row is committed. The downstream call intentionally re-enters the
    # full idempotent pipeline after this commit; its duplicate raw insert is harmless.
    inserted = TelegramListenerManager._persist_message(manager, captured)
    if inserted:
        _submit_processing(manager, captured, downstream, kind="message")
    return inserted


def _fast_persist_edit(
    manager: Any,
    captured: Any,
    downstream: Callable[[Any, Any], Any],
) -> bool:
    from app.telegram_listener_day13 import Day13TelegramListenerManager

    inserted = Day13TelegramListenerManager._persist_edit(manager, captured)
    if inserted:
        _submit_processing(manager, captured, downstream, kind="edit")
    return inserted


def install_telegram_fast_ingress() -> None:
    """Install save-first, process-after live push handling on the production listener base."""
    from app.telegram_listener_day28 import Day28TelegramListenerManager

    cls = Day28TelegramListenerManager

    original_init = cls.__init__
    if not getattr(original_init, "_telegram_fast_ingress_installed", False):
        def wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            self._telegram_fast_ingress_pool = _SourceProcessingPool()

        wrapped_init._telegram_fast_ingress_installed = True  # type: ignore[attr-defined]
        cls.__init__ = wrapped_init

    original_message = cls._persist_message
    if not getattr(original_message, "_telegram_fast_ingress_installed", False):
        def persist_message(self: Any, captured: Any) -> bool:
            return _fast_persist_message(self, captured, original_message)

        persist_message._telegram_fast_ingress_installed = True  # type: ignore[attr-defined]
        if getattr(original_message, "_telegram_revision_serialized", False):
            persist_message._telegram_revision_serialized = True  # type: ignore[attr-defined]
        cls._persist_message = persist_message

    original_edit = cls._persist_edit
    if not getattr(original_edit, "_telegram_fast_ingress_installed", False):
        def persist_edit(self: Any, captured: Any) -> bool:
            return _fast_persist_edit(self, captured, original_edit)

        persist_edit._telegram_fast_ingress_installed = True  # type: ignore[attr-defined]
        if getattr(original_edit, "_telegram_revision_serialized", False):
            persist_edit._telegram_revision_serialized = True  # type: ignore[attr-defined]
        cls._persist_edit = persist_edit

    original_stop = cls.stop
    if not getattr(original_stop, "_telegram_fast_ingress_installed", False):
        async def stop(self: Any) -> None:
            try:
                await original_stop(self)
            finally:
                pool = getattr(self, "_telegram_fast_ingress_pool", None)
                if pool is not None:
                    pool.shutdown()

        stop._telegram_fast_ingress_installed = True  # type: ignore[attr-defined]
        cls.stop = stop


__all__ = [
    "_SourceProcessingPool",
    "_fast_persist_edit",
    "_fast_persist_message",
    "install_telegram_fast_ingress",
]
