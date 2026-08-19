"""Canonical production Telegram listener.

This module owns live provider ingress.  It deliberately contains no import-time
monkey patching.  Raw Telegram evidence is committed first, then one FIFO worker per
provider processes classification/AI/canonical execution.  Rapid edits are processed
by the exact revision that was just committed, never by a later MAX(revision_index).

Different providers remain concurrent.  Replays/recovery continue to use the accepted
Day 38 recovery policy inherited from PaperPendingAwareListenerManager until that
policy is folded into this canonical module during the Day 37-38 cleanup.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from threading import Lock, RLock
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import TelegramListenerManager
from app.telegram_listener_day13 import Day13TelegramListenerManager
from app.telegram_listener_day38 import (
    PaperPendingAwareListenerManager,
    _build_pending_reconciler,
    build_day38_execution_router_from_env,
)

logger = logging.getLogger(__name__)
_STRIPE_COUNT = 128


class _ProviderProcessingPool:
    """Exactly one ordered downstream worker per provider source."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._closed = False

    def submit(self, source_id: object, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        key = str(source_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("telegram_ingress_closed")
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


class CanonicalProductionTelegramListenerManager(PaperPendingAwareListenerManager):
    """Single live ingress implementation used by production."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._telegram_processing_pool = _ProviderProcessingPool()
        self._telegram_revision_locks = tuple(RLock() for _ in range(_STRIPE_COUNT))

    def _revision_lock(self, source_id: object, telegram_message_id: int) -> RLock:
        index = hash((str(source_id), int(telegram_message_id))) % _STRIPE_COUNT
        return self._telegram_revision_locks[index]

    @staticmethod
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
                "Telegram downstream processing failed kind=%s source=%s message=%s",
                kind,
                source_id,
                telegram_message_id,
            )

    def _submit(self, captured: Any, fn: Callable[..., Any], *args: Any, kind: str) -> None:
        future = self._telegram_processing_pool.submit(captured.source_id, fn, *args)
        future.add_done_callback(
            lambda done: self._report_future(
                done,
                source_id=captured.source_id,
                telegram_message_id=int(captured.telegram_message_id),
                kind=kind,
            )
        )

    def _persist_message(self, captured: Any) -> bool:
        """Commit immutable raw evidence before any slow downstream work."""
        with self._revision_lock(captured.source_id, captured.telegram_message_id):
            inserted = TelegramListenerManager._persist_message(self, captured)
        if inserted:
            self._submit(captured, self._process_saved_original, captured, kind="message-r0")
        return inserted

    def _process_saved_original(self, captured: Any) -> Any:
        # Re-enter the accepted Day 21-28 downstream chain after the raw row exists.
        # Its duplicate raw insert is intentionally harmless; classification, AI,
        # canonicalization and dispatch still run.  This dependency is removed when
        # Days 14-28 are folded into the canonical runtime in the next cleanup block.
        return super()._persist_message(captured)

    def _persist_edit(self, captured: Any) -> bool:
        """Commit one append-only revision and process exactly that revision."""
        with self._revision_lock(captured.source_id, captured.telegram_message_id):
            inserted = Day13TelegramListenerManager._persist_edit(self, captured)
            if not inserted:
                return False
            revision_index = self._exact_saved_revision_index(captured)

        if revision_index is None or revision_index <= 0:
            logger.error(
                "Telegram saved edit revision unresolved source=%s message=%s",
                captured.source_id,
                captured.telegram_message_id,
            )
            return True

        self._submit(
            captured,
            self._process_saved_edit,
            captured,
            revision_index,
            kind=f"edit-r{revision_index}",
        )
        return True

    def _exact_saved_revision_index(self, captured: Any) -> int | None:
        content_hash = sha256(str(captured.raw_text or "").encode("utf-8")).hexdigest()
        with self._session_factory_day28() as session:
            value = session.execute(
                text(
                    """
                    SELECT mr.revision_index
                    FROM messages m
                    JOIN message_revisions mr ON mr.message_id = m.id
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

    def _process_saved_edit(self, captured: Any, revision_index: int) -> Any:
        pipeline = getattr(self, "_ai_pipeline", None)
        exact_processor = getattr(pipeline, "_process_revision", None) if pipeline is not None else None
        if callable(exact_processor):
            result = exact_processor(
                captured.source_id,
                int(captured.telegram_message_id),
                revision_index=revision_index,
            )
            self._dispatch_sync(
                source_id=captured.source_id,
                telegram_message_id=int(captured.telegram_message_id),
                revision_index=revision_index,
            )
            return result

        # Non-AI/test configurations retain the accepted fallback chain.
        result = super()._persist_edit(captured)
        self._dispatch_sync(
            source_id=captured.source_id,
            telegram_message_id=int(captured.telegram_message_id),
            revision_index=revision_index,
        )
        return result

    async def stop(self) -> None:
        try:
            await super().stop()
        finally:
            self._telegram_processing_pool.shutdown()


def build_canonical_production_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
    excluded_chat_id: int | None,
) -> CanonicalProductionTelegramListenerManager:
    router = build_day38_execution_router_from_env(session_factory=session_factory)
    return CanonicalProductionTelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
        excluded_chat_id=excluded_chat_id,
        day28_router=router,
        pending_reconciler=_build_pending_reconciler(
            session_factory=session_factory,
            router=router,
        ),
    )


__all__ = [
    "CanonicalProductionTelegramListenerManager",
    "build_canonical_production_listener_manager",
]
