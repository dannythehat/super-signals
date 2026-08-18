"""Day 38 listener wiring for independent Owner/member execution.

Reliability invariant: a reconnect/restart may make a NEW entry stale, but it must not
silently discard an explicit management instruction for a mapped trade. Recovered
close/SL/BE/partial/cancel decisions are therefore broker-routed regardless of age;
recovered new trades retain the bounded paper freshness gate.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.day38_database_source_router import DatabaseSourceDay38FullExecutionRouter
from app.literal_management_overrides import install_literal_management_overrides
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_execution_day38 import Day38LiveUserExecutionService
from app.mt5_management_day38 import Day38LiveUserManagementService
from app.paper_critical_management_v2 import PaperCriticalManagementV2
from app.paper_fresh_run_reset import install_paper_fresh_run_reset
from app.paper_fresh_start_execution import PaperFreshStartExecutionService
from app.paper_pending_reconciler import PaperPendingReconciler
from app.paper_resilient_read_gateway import PaperResilientMetaApiReadGateway
from app.paper_safe_member_routing import (
    PaperSafeMemberDistribution,
    PaperSafeMemberManagement,
)
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage, ReaderListeningPlan
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day21 import Day21TelegramListenerManager
from app.telegram_listener_day28 import Day28TelegramListenerManager

logger = logging.getLogger(__name__)
_RECOVERY_HISTORY_LIMIT = 50
_DEFAULT_PAPER_RECOVERY_AGE_SECONDS = 90.0
_RECOVERY_CLOCK_SKEW_SECONDS = 5.0


def _enabled(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _broker_keys() -> tuple[str, ...]:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def build_day38_execution_router_from_env(
    *,
    session_factory: sessionmaker[Session],
) -> DatabaseSourceDay38FullExecutionRouter | None:
    """Build automatic execution using durable DB source status as source eligibility."""
    install_paper_fresh_run_reset()

    if not _enabled(os.getenv("SUPER_SIGNALS_DAY28_AUTO_EXECUTION_ENABLED")):
        return None
    try:
        owner_user_id = UUID(os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip())
    except ValueError:
        logger.error("Day 38 automatic execution disabled: invalid owner UUID configuration")
        return None

    broker_keys = _broker_keys()
    if not broker_keys:
        logger.error("Day 38 automatic execution disabled: broker encryption keys are unavailable")
        return None

    risk_percent = os.getenv("SUPER_SIGNALS_DAY28_RISK_PERCENT", "1").strip() or "1"
    double_lot_approved = _enabled(
        os.getenv("SUPER_SIGNALS_DAY28_ALLOW_DOUBLE_LOT"),
        default=True,
    )

    try:
        install_literal_management_overrides()

        cipher = MetaApiTokenCipher(broker_keys)
        owner_read_gateway = PaperResilientMetaApiReadGateway()
        member_read_gateway = MetaApiReadGateway()
        trade_gateway = MetaApiTradeGateway()
        margin_gateway = MetaApiMarginGateway()

        owner_execution = PaperFreshStartExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=owner_read_gateway,
            margin_gateway=margin_gateway,
            trade_gateway=trade_gateway,
        )
        owner_management = PaperCriticalManagementV2(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=owner_read_gateway,
            trade_gateway=trade_gateway,
        )
        member_execution = Day38LiveUserExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read_gateway,
            margin_gateway=margin_gateway,
            trade_gateway=trade_gateway,
        )
        member_management = Day38LiveUserManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read_gateway,
            trade_gateway=trade_gateway,
        )
        return DatabaseSourceDay38FullExecutionRouter(
            session_factory=session_factory,
            owner_user_id=owner_user_id,
            execution_service=owner_execution,
            management_service=owner_management,
            member_distribution=PaperSafeMemberDistribution(
                session_factory=session_factory,
                execution_service=member_execution,
            ),
            member_management=PaperSafeMemberManagement(
                session_factory=session_factory,
                management_service=member_management,
            ),
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )
    except ValueError as exc:
        logger.error("Day 38 automatic execution disabled: %s", str(exc))
        return None


class PaperPendingAwareListenerManager(Day28TelegramListenerManager):
    """Proven listener plus pending observation and management-priority recovery."""

    def __init__(self, *, pending_reconciler: PaperPendingReconciler | None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._paper_pending_reconciler = pending_reconciler

    async def start(self) -> None:
        if self._paper_pending_reconciler is not None:
            await self._paper_pending_reconciler.start()
        try:
            await super().start()
        except Exception:
            if self._paper_pending_reconciler is not None:
                await self._paper_pending_reconciler.stop()
            raise

    async def stop(self) -> None:
        try:
            await super().stop()
        finally:
            if self._paper_pending_reconciler is not None:
                await self._paper_pending_reconciler.stop()

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
                    str(_DEFAULT_PAPER_RECOVERY_AGE_SECONDS),
                )
            )
        except (TypeError, ValueError):
            max_age = _DEFAULT_PAPER_RECOVERY_AGE_SECONDS
        if max_age <= 0:
            max_age = _DEFAULT_PAPER_RECOVERY_AGE_SECONDS
        age = (reference.astimezone(UTC) - value_utc.astimezone(UTC)).total_seconds()
        return -_RECOVERY_CLOCK_SKEW_SECONDS <= age <= max_age

    async def _dispatch_recovered_if_required(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
        occurred_at: datetime,
    ) -> None:
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

        is_management = stored.decision == "trade_update" and stored.action == "apply_update"
        is_fresh_entry = (
            stored.decision == "new_trade"
            and stored.action == "execute"
            and self._fresh_recovered_entry(occurred_at)
        )
        if not is_management and not is_fresh_entry:
            logger.info(
                "Recovered Telegram message retained as evidence only decision=%s action=%s",
                stored.decision,
                stored.action,
                extra={
                    "source_id": str(source_id),
                    "telegram_message_id": telegram_message_id,
                    "revision_index": revision_index,
                },
            )
            return

        # Management deliberately has no age veto here. The durable lifecycle event
        # and broker mapping decide whether anything remains to manage; if it is
        # already closed/applied the router is idempotent and sends no duplicate order.
        await asyncio.to_thread(
            self._dispatch_sync,
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )

    async def _recover_live_gaps(
        self,
        client,
        plan: ReaderListeningPlan,
    ) -> None:
        """Recover missed push events; management outranks message age."""
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
                    reply_to_message_id=(
                        int(reply_to_message_id) if reply_to_message_id is not None else None
                    ),
                    has_media=media is not None,
                    media_type=type(media).__name__ if media is not None else None,
                )
                inserted = await asyncio.to_thread(
                    Day21TelegramListenerManager._persist_message,
                    self,
                    captured,
                )
                if inserted:
                    await self._dispatch_recovered_if_required(
                        source_id=source.source_id,
                        telegram_message_id=int(message_id),
                        revision_index=0,
                        occurred_at=posted_at,
                    )
                    continue

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
                edited = await asyncio.to_thread(
                    Day21TelegramListenerManager._persist_edit,
                    self,
                    captured_edit,
                )
                if not edited:
                    continue
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

    async def _catch_up_recent_messages(
        self,
        client,
        plan: ReaderListeningPlan,
    ) -> None:
        """Restart catch-up: stale entries stay evidence; management is recovered."""
        for source in plan.sources:
            try:
                messages = await client.get_messages(source.chat_id, limit=_RECOVERY_HISTORY_LIMIT)
            except Exception:
                logger.exception(
                    "Telegram catch-up skipped one unreadable source",
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
                    reply_to_message_id=(
                        int(reply_to_message_id) if reply_to_message_id is not None else None
                    ),
                    has_media=media is not None,
                    media_type=type(media).__name__ if media is not None else None,
                )
                await asyncio.to_thread(
                    Day21TelegramListenerManager._persist_message,
                    self,
                    captured,
                )

                # Dispatch even when already persisted: this intentionally repairs the
                # old Day37 evidence-only hole after a process restart. Router/lifecycle
                # idempotency prevents duplicate broker mutation.
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
                await asyncio.to_thread(
                    Day21TelegramListenerManager._persist_edit,
                    self,
                    captured_edit,
                )
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


def _build_pending_reconciler(
    *,
    session_factory: sessionmaker[Session],
    router: DatabaseSourceDay38FullExecutionRouter | None,
) -> PaperPendingReconciler | None:
    if router is None:
        return None
    try:
        owner_user_id = UUID(os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip())
        poll_seconds = int(
            os.getenv("SUPER_SIGNALS_PAPER_PENDING_POLL_SECONDS", "3").strip() or "3"
        )
        broker_keys = _broker_keys()
        if not broker_keys:
            return None
        return PaperPendingReconciler(
            session_factory=session_factory,
            cipher=MetaApiTokenCipher(broker_keys),
            gateway=PaperResilientMetaApiReadGateway(),
            owner_user_id=owner_user_id,
            poll_seconds=poll_seconds,
        )
    except (ValueError, TypeError):
        logger.error("Paper pending reconciler disabled: invalid owner/key/poll configuration")
        return None


def build_day38_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
    excluded_chat_id: int | None,
) -> Day28TelegramListenerManager:
    router = build_day38_execution_router_from_env(session_factory=session_factory)
    return PaperPendingAwareListenerManager(
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
    "PaperPendingAwareListenerManager",
    "build_day38_execution_router_from_env",
    "build_day38_listener_manager",
]
