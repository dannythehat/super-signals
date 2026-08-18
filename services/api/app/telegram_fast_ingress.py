"""Fast live Telegram ingress for production paper testing.

The live event handler durably persists the provider message or edit first. Classification,
AI supervision, canonical signal creation and broker dispatch then run on an ordered
per-source worker so slow downstream work never blocks Telethon receipt.

Edits need one extra guarantee: the background worker must process the exact revision
which the fast ingress just committed. Re-entering the normal edit persistence chain
would attempt to insert the same edit twice; the duplicate is correctly rejected and the
old pipeline then mistakes that rejection for "nothing to process". Exact-revision
processing prevents that silent trade loss and also prevents a later edit from overtaking
the revision that triggered the worker.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from threading import Lock
from typing import Any, Callable

from sqlalchemy import text

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


def _submit_exact_edit_processing(
    manager: Any,
    captured: Any,
    revision_index: int,
    fallback_downstream: Callable[[Any, Any], Any],
) -> None:
    future = _pool_for(manager).submit(
        captured.source_id,
        _process_exact_saved_edit,
        manager,
        captured,
        revision_index,
        fallback_downstream,
    )
    future.add_done_callback(
        lambda done: _report_future(
            done,
            source_id=captured.source_id,
            telegram_message_id=int(captured.telegram_message_id),
            kind=f"edit-r{revision_index}",
        )
    )


def _exact_saved_revision_index(manager: Any, captured: Any) -> int | None:
    """Resolve the exact revision committed by the just-completed raw edit save."""
    session_factory = getattr(manager, "_session_factory", None)
    if session_factory is None:
        session_factory = getattr(manager, "_session_factory_day28", None)
    if session_factory is None:
        return None

    content_hash = sha256(str(captured.raw_text or "").encode("utf-8")).hexdigest()
    with session_factory() as session:
        value = session.execute(
            text(
                """
                SELECT mr.revision_index
                FROM messages m
                JOIN message_revisions mr ON mr.message_id=m.id
                WHERE m.source_id=:source_id
                  AND m.telegram_message_id=:telegram_message_id
                  AND mr.content_sha256=:content_sha256
                  AND mr.edited_at=:edited_at
                ORDER BY mr.revision_index DESC
                LIMIT 1
                """
            ),
            {
                "source_id": captured.source_id,
                "telegram_message_id": int(captured.telegram_message_id),
                "content_sha256": content_hash,
                "edited_at": captured.edited_at,
            },
        ).scalar_one_or_none()
    return int(value) if value is not None else None


def _process_exact_saved_edit(
    manager: Any,
    captured: Any,
    revision_index: int,
    fallback_downstream: Callable[[Any, Any], Any],
) -> Any:
    """Process and dispatch exactly the revision already persisted by fast ingress."""
    pipeline = getattr(manager, "_ai_pipeline", None)
    exact_processor = getattr(pipeline, "_process_revision", None) if pipeline is not None else None
    dispatch = getattr(manager, "_dispatch_sync", None)

    if callable(exact_processor):
        result = exact_processor(
            captured.source_id,
            int(captured.telegram_message_id),
            revision_index=revision_index,
        )
        if callable(dispatch):
            dispatch(
                source_id=captured.source_id,
                telegram_message_id=int(captured.telegram_message_id),
                revision_index=revision_index,
            )
        return result

    # Legacy/test configurations without the AI decision pipeline retain their old
    # downstream lifecycle behaviour. If a broker router exists, dispatch the exact
    # revision after that fallback rather than relying on a MAX(revision_index) lookup.
    result = fallback_downstream(manager, captured)
    if callable(dispatch):
        dispatch(
            source_id=captured.source_id,
            telegram_message_id=int(captured.telegram_message_id),
            revision_index=revision_index,
        )
    return result


def _fast_persist_message(
    manager: Any,
    captured: Any,
    downstream: Callable[[Any, Any], Any],
) -> bool:
    from app.telegram_listener import TelegramListenerManager

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
        revision_index = _exact_saved_revision_index(manager, captured)
        if revision_index is None or revision_index <= 0:
            logger.error(
                "Telegram saved edit revision could not be resolved source=%s message=%s",
                captured.source_id,
                captured.telegram_message_id,
            )
            return True
        _submit_exact_edit_processing(
            manager,
            captured,
            revision_index,
            downstream,
        )
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
    "_exact_saved_revision_index",
    "_fast_persist_edit",
    "_fast_persist_message",
    "_process_exact_saved_edit",
    "install_telegram_fast_ingress",
]
