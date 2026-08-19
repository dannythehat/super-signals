"""Canonical production Telegram listener.

This module owns live provider ingress and recovery.  It deliberately contains no
import-time monkey patching.  Raw Telegram evidence is committed first, then one FIFO
worker per provider processes classification/AI/canonical execution.  Rapid edits are
processed by the exact revision that was just committed, never by a later
MAX(revision_index).

Different providers remain concurrent.  Connected recovery treats PostgreSQL as the
durable hand-off: every still-fresh actionable message is reconsidered by the
idempotent router even when another handler already persisted it.  Unresolved
management evidence is not spam-routed repeatedly.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from threading import Lock, RLock
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage, ReaderListeningPlan, TelegramListenerManager
from app.telegram_listener_day13 import CapturedTelegramEdit, Day13TelegramListenerManager
from app.telegram_listener_day21 import Day21TelegramListenerManager
from app.telegram_listener_day38 import (
    PaperPendingAwareListenerManager,
    _build_pending_reconciler,
    build_day38_execution_router_from_env,
)

logger = logging.getLogger(__name__)
_STRIPE_COUNT = 128
_RECOVERY_HISTORY_LIMIT = 50


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
    """Single live ingress/recovery implementation used by production."""

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
        # Re-enter the accepted downstream chain after the raw row exists.  Its
        # duplicate raw insert is harmless; classification, AI, canonicalisation and
        # dispatch still run.  The day-numbered dependency is removed as those stages
        # are folded into the canonical runtime.
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

        result = super()._persist_edit(captured)
        self._dispatch_sync(
            source_id=captured.source_id,
            telegram_message_id=int(captured.telegram_message_id),
            revision_index=revision_index,
        )
        return result

    async def _dispatch_recovered_if_required(
        self,
        *,
        source_id,
        telegram_message_id: int,
        revision_index: int,
        occurred_at,
    ) -> None:
        """Route durable actionable recovery without repeating unresolved management."""
        router = self._day28_router
        if router is None:
            return
        stored = await asyncio.to_thread(
            router._load_stored_decision,
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )
        if stored is None:
            return

        if stored.decision == "trade_update" and stored.action == "apply_update":
            resolver = getattr(router, "_resolve_lifecycle_event", None)
            if resolver is not None:
                lifecycle_event_id, signal_id = await asyncio.to_thread(
                    resolver,
                    stored.message_id,
                    revision_index,
                )
                if lifecycle_event_id is None or signal_id is None:
                    logger.info(
                        "Recovered management retained as evidence: lifecycle target unresolved",
                        extra={
                            "source_id": str(source_id),
                            "telegram_message_id": telegram_message_id,
                            "revision_index": revision_index,
                        },
                    )
                    return

        await super()._dispatch_recovered_if_required(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            occurred_at=occurred_at,
        )

    async def _recover_live_gaps(self, client: Any, plan: ReaderListeningPlan) -> None:
        """Repair fresh persisted-but-unrouted decisions as well as missed inserts."""
        for source in plan.sources:
            try:
                messages = await client.get_messages(source.chat_id, limit=_RECOVERY_HISTORY_LIMIT)
            except Exception:
                logger.exception(
                    "Telegram live recovery skipped one unreadable source",
                    extra={"source_id": str(source.source_id), "chat_id": source.chat_id},
                )
                continue

            for message in reversed(list(messages)):
                message_id = getattr(message, "id", None)
                if message_id is None:
                    continue
                reply_to = getattr(message, "reply_to", None)
                reply_to_message_id = getattr(reply_to, "reply_to_msg_id", None)
                media = getattr(message, "media", None)
                raw_text = str(getattr(message, "raw_text", "") or "")
                posted_at = self._utc_datetime(getattr(message, "date", None))
                captured = CapturedTelegramMessage(
                    source_id=source.source_id,
                    chat_id=source.chat_id,
                    telegram_message_id=int(message_id),
                    raw_text=raw_text,
                    posted_at=posted_at,
                    reply_to_message_id=(int(reply_to_message_id) if reply_to_message_id is not None else None),
                    has_media=media is not None,
                    media_type=type(media).__name__ if media is not None else None,
                )

                # Run persistence + supervision, then always consult the durable
                # decision.  This closes the crash window between AI completion and
                # broker dispatch even if the message row already existed.
                await asyncio.to_thread(Day21TelegramListenerManager._persist_message, self, captured)
                await self._dispatch_recovered_if_required(
                    source_id=source.source_id,
                    telegram_message_id=int(message_id),
                    revision_index=0,
                    occurred_at=posted_at,
                )

                edit_date = getattr(message, "edit_date", None)
                if edit_date is None:
                    continue
                edited_at = self._utc_datetime(edit_date)
                captured_edit = CapturedTelegramEdit(
                    source_id=source.source_id,
                    chat_id=source.chat_id,
                    telegram_message_id=int(message_id),
                    raw_text=raw_text,
                    edited_at=edited_at,
                    reply_to_message_id=(int(reply_to_message_id) if reply_to_message_id is not None else None),
                    has_media=media is not None,
                    media_type=type(media).__name__ if media is not None else None,
                )
                await asyncio.to_thread(Day21TelegramListenerManager._persist_edit, self, captured_edit)
                revision_index = await asyncio.to_thread(
                    self._latest_revision_index,
                    source.source_id,
                    int(message_id),
                )
                if revision_index > 0:
                    await self._dispatch_recovered_if_required(
                        source_id=source.source_id,
                        telegram_message_id=int(message_id),
                        revision_index=revision_index,
                        occurred_at=edited_at,
                    )

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
