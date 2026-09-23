"""Canonical production Telegram listener.

One production path owns provider ingress, exact edit ordering, AI/canonical processing,
fresh gap recovery and broker dispatch. Raw Telegram evidence is committed first. Slow
downstream work is FIFO per provider while different providers remain concurrent.

Recovery is deliberately forward-only. It may rescue a genuinely fresh Telegram message
that push delivery missed, but it never replays historical trade or management decisions.
A stored management revision that has already reached the broker router is terminal and
is never offered again on subsequent recovery scans.
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock, RLock
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.execution_router_canonical import (
    build_canonical_execution_router,
    build_canonical_pending_reconciler,
    build_management_reliability_runtime,
)
from app.management_reliability_runtime import ManagementReliabilityRuntime
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_entity_recovery import read_messages_with_entity_recovery
from app.telegram_listener import CapturedTelegramMessage, ReaderListeningPlan, TelegramListenerManager
from app.telegram_listener_day13 import CapturedTelegramEdit, Day13TelegramListenerManager
from app.telegram_listener_day21 import Day21TelegramListenerManager
from app.unified_pending_reconciler import UnifiedPendingReconciler

logger = logging.getLogger(__name__)
_STRIPE_COUNT = 128
_RECOVERY_HISTORY_LIMIT = 50
_RECOVERY_CONCURRENCY = 4
_DEFAULT_RECOVERY_AGE_SECONDS = 90.0
_RECOVERY_CLOCK_SKEW_SECONDS = 5.0


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


class CanonicalProductionTelegramListenerManager(Day21TelegramListenerManager):
    """Single live ingress/fresh-recovery/broker-dispatch implementation."""

    def __init__(
        self,
        *,
        canonical_router,
        pending_reconciler: UnifiedPendingReconciler | None,
        management_reliability_runtime: ManagementReliabilityRuntime | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._canonical_router = canonical_router
        self._canonical_pending_reconciler = pending_reconciler
        self._management_reliability_runtime = management_reliability_runtime
        self._telegram_processing_pool = _ProviderProcessingPool()
        self._telegram_revision_locks = tuple(RLock() for _ in range(_STRIPE_COUNT))

    async def start(self) -> None:
        """Start only the canonical runtime, never inherited Day-numbered backfills.

        The historical listener classes remain temporary implementation dependencies
        for reader mechanics, but their start hooks include parser/review/signal/lifecycle
        backfills from earlier build generations. Production must not run those hooks.
        """
        if self._canonical_pending_reconciler is not None:
            await self._canonical_pending_reconciler.start()
        if self._management_reliability_runtime is not None:
            await self._management_reliability_runtime.start()
        try:
            await TelegramListenerManager.start(self)
        except Exception:
            if self._management_reliability_runtime is not None:
                await self._management_reliability_runtime.stop()
            if self._canonical_pending_reconciler is not None:
                await self._canonical_pending_reconciler.stop()
            raise

    async def stop(self) -> None:
        try:
            await TelegramListenerManager.stop(self)
        finally:
            if self._management_reliability_runtime is not None:
                await self._management_reliability_runtime.stop()
            if self._canonical_pending_reconciler is not None:
                await self._canonical_pending_reconciler.stop()
            self._telegram_processing_pool.shutdown()

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
        pipeline = getattr(self, "_ai_pipeline", None)
        result = None
        if pipeline is not None:
            result = pipeline.process_original(captured.source_id, int(captured.telegram_message_id))
        self._dispatch_sync(
            source_id=captured.source_id,
            telegram_message_id=int(captured.telegram_message_id),
            revision_index=0,
        )
        return result

    def _persist_edit(self, captured: Any) -> bool:
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
        with self._session_factory() as session:
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
        if not callable(exact_processor):
            raise RuntimeError("canonical_ai_revision_processor_missing")
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

    def _dispatch_sync(
        self,
        *,
        source_id,
        telegram_message_id: int,
        revision_index: int,
    ) -> None:
        router = self._canonical_router
        if router is None:
            return
        # Provider work is already FIFO inside each provider-specific executor and
        # the canonical router applies per-signal/event idempotency locks. Do not add a
        # process-wide dispatch lock here: one slow MetaAPI call would otherwise freeze
        # every unrelated provider behind it.
        try:
            result = asyncio.run(
                router.dispatch_stored_decision(
                    source_id=source_id,
                    telegram_message_id=telegram_message_id,
                    revision_index=revision_index,
                )
            )
        except Exception:
            logger.exception(
                "Canonical broker dispatch failed unexpectedly",
                extra={
                    "source_id": str(source_id),
                    "telegram_message_id": telegram_message_id,
                    "revision_index": revision_index,
                },
            )
            return

        if result.outcome == "blocked":
            logger.warning(
                "Canonical broker route blocked code=%s",
                result.error_code or result.reason,
                extra={
                    "source_id": str(source_id),
                    "telegram_message_id": telegram_message_id,
                    "revision_index": revision_index,
                },
            )
        elif result.outcome in {"executed", "managed"}:
            logger.info(
                "Canonical broker route completed outcome=%s positions=%d broker_actions=%d",
                result.outcome,
                result.position_count,
                result.broker_actions_sent,
                extra={
                    "source_id": str(source_id),
                    "telegram_message_id": telegram_message_id,
                    "revision_index": revision_index,
                    "signal_id": str(result.signal_id) if result.signal_id else None,
                },
            )

    @staticmethod
    def _fresh_recovered_entry(value: datetime, *, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        value_utc = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        try:
            max_age = float(
                os.getenv(
                    "SUPER_SIGNALS_PAPER_MAX_SIGNAL_AGE_SECONDS",
                    str(_DEFAULT_RECOVERY_AGE_SECONDS),
                )
            )
        except (TypeError, ValueError):
            max_age = _DEFAULT_RECOVERY_AGE_SECONDS
        if max_age <= 0:
            max_age = _DEFAULT_RECOVERY_AGE_SECONDS
        age = (reference.astimezone(UTC) - value_utc.astimezone(UTC)).total_seconds()
        return -_RECOVERY_CLOCK_SKEW_SECONDS <= age <= max_age

    def _management_route_already_attempted(self, lifecycle_event_id: object) -> bool:
        """Terminal recovery guard for the exact provider lifecycle event."""
        with self._session_factory() as session:
            return bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM audit_events
                            WHERE event_type IN ('mt5.day28_route_success','mt5.day28_route_failure')
                              AND payload->>'lifecycle_event_id'=:lifecycle_event_id
                        )
                        """
                    ),
                    {"lifecycle_event_id": str(lifecycle_event_id)},
                ).scalar_one()
            )

    def _entry_route_already_attempted(self, signal_id: object) -> bool:
        """A recovered entry is terminal once the canonical new-trade router was tried."""
        with self._session_factory() as session:
            return bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM audit_events
                            WHERE event_type='mt5.day38_route_new_trade'
                              AND entity_type='signal'
                              AND entity_id=:signal_id
                        )
                        """
                    ),
                    {"signal_id": str(signal_id)},
                ).scalar_one()
            )

    def _entry_superseded_by_newer_signal(self, signal_id: object) -> bool:
        """Never replay an older provider entry after a newer accepted signal exists."""
        with self._session_factory() as session:
            return bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM signals AS newer
                            JOIN signals AS current ON current.id=:signal_id
                            WHERE newer.source_id=current.source_id
                              AND newer.parser_status='accepted'
                              AND newer.id<>current.id
                              AND newer.created_at>current.created_at
                        )
                        """
                    ),
                    {"signal_id": str(signal_id)},
                ).scalar_one()
            )

    async def _dispatch_recovered_if_required(
        self,
        *,
        source_id,
        telegram_message_id: int,
        revision_index: int,
        occurred_at: datetime,
    ) -> None:
        """Recover only a fresh unattempted actionable decision; history is evidence only."""
        router = self._canonical_router
        if router is None:
            return
        if not self._fresh_recovered_entry(occurred_at):
            return

        stored = await asyncio.to_thread(
            router._load_stored_decision,
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )
        if stored is None:
            return

        is_management = stored.decision == "trade_update" and stored.action == "apply_update"
        is_entry = stored.decision == "new_trade" and stored.action == "execute"
        if not is_management and not is_entry:
            return

        if is_management:
            resolver = getattr(router, "_resolve_lifecycle_event", None)
            if resolver is None:
                return
            lifecycle_event_id, signal_id = await asyncio.to_thread(
                resolver,
                stored.message_id,
                revision_index,
            )
            if lifecycle_event_id is None or signal_id is None:
                return
            attempted = await asyncio.to_thread(
                self._management_route_already_attempted,
                lifecycle_event_id,
            )
            if attempted:
                return

        if is_entry:
            resolver = getattr(router, "_resolve_signal_id", None)
            if resolver is None:
                return
            signal_id = await asyncio.to_thread(
                resolver,
                stored.message_id,
                revision_index,
            )
            if signal_id is None:
                return
            attempted = await asyncio.to_thread(
                self._entry_route_already_attempted,
                signal_id,
            )
            if attempted:
                return
            superseded = await asyncio.to_thread(
                self._entry_superseded_by_newer_signal,
                signal_id,
            )
            if superseded:
                logger.info(
                    "Recovered entry skipped because newer provider signal exists signal=%s",
                    signal_id,
                )
                return

        await asyncio.to_thread(
            self._dispatch_sync,
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )

    def _persist_recovered_original(self, captured: CapturedTelegramMessage) -> bool:
        """Persist and supervise only when recovery truly discovers a missing row."""
        with self._revision_lock(captured.source_id, captured.telegram_message_id):
            inserted = TelegramListenerManager._persist_message(self, captured)
        if inserted:
            pipeline = getattr(self, "_ai_pipeline", None)
            if pipeline is not None:
                pipeline.process_original(captured.source_id, int(captured.telegram_message_id))
        return inserted

    def _persist_recovered_edit(self, captured: CapturedTelegramEdit) -> tuple[bool, int]:
        with self._revision_lock(captured.source_id, captured.telegram_message_id):
            inserted = Day13TelegramListenerManager._persist_edit(self, captured)
            revision_index = self._exact_saved_revision_index(captured) or 0
        if inserted and revision_index > 0:
            pipeline = getattr(self, "_ai_pipeline", None)
            exact_processor = getattr(pipeline, "_process_revision", None) if pipeline is not None else None
            if callable(exact_processor):
                exact_processor(
                    captured.source_id,
                    int(captured.telegram_message_id),
                    revision_index=revision_index,
                )
        return inserted, revision_index

    async def _recover_source_gap(self, client: Any, source: Any) -> None:
        """Recover recent missed push delivery without replaying persisted history."""
        try:
            messages = await read_messages_with_entity_recovery(
                client,
                source.chat_id,
                limit=_RECOVERY_HISTORY_LIMIT,
            )
        except Exception:
            logger.exception(
                "Telegram recovery skipped one unreadable source",
                extra={"source_id": str(source.source_id), "chat_id": source.chat_id},
            )
            return

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
                reply_to_message_id=(
                    int(reply_to_message_id) if reply_to_message_id is not None else None
                ),
                has_media=media is not None,
                media_type=type(media).__name__ if media is not None else None,
            )

            await asyncio.to_thread(self._persist_recovered_original, captured)
            # Dispatch is guarded by freshness + exact route-attempt evidence, so an
            # already-persisted message can recover the narrow crash window between
            # interpretation commit and broker dispatch without duplicating a trade.
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
                reply_to_message_id=(
                    int(reply_to_message_id) if reply_to_message_id is not None else None
                ),
                has_media=media is not None,
                media_type=type(media).__name__ if media is not None else None,
            )
            _inserted_edit, revision_index = await asyncio.to_thread(
                self._persist_recovered_edit,
                captured_edit,
            )
            if revision_index > 0:
                await self._dispatch_recovered_if_required(
                    source_id=source.source_id,
                    telegram_message_id=int(message_id),
                    revision_index=revision_index,
                    occurred_at=edited_at,
                )

    async def _recover_live_gaps(self, client: Any, plan: ReaderListeningPlan) -> None:
        semaphore = asyncio.Semaphore(_RECOVERY_CONCURRENCY)

        async def bounded(source: Any) -> None:
            async with semaphore:
                await self._recover_source_gap(client, source)

        await asyncio.gather(*(bounded(source) for source in plan.sources))

    async def _catch_up_recent_messages(self, client: Any, plan: ReaderListeningPlan) -> None:
        await self._recover_live_gaps(client, plan)


def build_canonical_production_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
    excluded_chat_id: int | None,
) -> CanonicalProductionTelegramListenerManager:
    router = build_canonical_execution_router(session_factory=session_factory)
    return CanonicalProductionTelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
        excluded_chat_id=excluded_chat_id,
        canonical_router=router,
        pending_reconciler=build_canonical_pending_reconciler(
            session_factory=session_factory,
            router=router,
        ),
        management_reliability_runtime=build_management_reliability_runtime(
            session_factory=session_factory,
            router=router,
        ),
    )


__all__ = [
    "CanonicalProductionTelegramListenerManager",
    "build_canonical_production_listener_manager",
]
