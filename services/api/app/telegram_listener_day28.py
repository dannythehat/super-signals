"""Day 28 listener adapter for the complete automatic execution gate.

Live Telegram messages still use the proven Day 21 persistence + AI/V1 pipeline.
Day 38 now supplies the execution router beneath this same listener so one canonical
Signal can fan out independently to eligible member LIVE accounts while the accepted
Owner demo reference path remains available. Bounded reconnect catch-up deliberately
remains supervision/audit-only so historical messages can never become fresh broker
instructions after a restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from uuid import UUID

from telethon import TelegramClient
from sqlalchemy.orm import Session, sessionmaker

from app.day28_full_execution import Day28FullExecutionRouter
from app.day28_zone_guard import Day28GuardedExecutionService
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_management_day27 import Day27Mt5ManagementService
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage, ReaderListeningPlan
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day21 import Day21TelegramListenerManager

logger = logging.getLogger(__name__)
_DAY28_LIVE_RECOVERY_LIMIT = 10
_DAY28_LIVE_RECOVERY_MAX_AGE_SECONDS = 60


def _enabled(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _uuid_list(raw: str) -> tuple[UUID, ...]:
    values: list[UUID] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        values.append(UUID(token))
    return tuple(values)


def build_day28_execution_router_from_env(
    *,
    session_factory: sessionmaker[Session],
) -> Day28FullExecutionRouter | None:
    """Retain the historical Owner-demo builder for tests/diagnostics."""
    if not _enabled(os.getenv("SUPER_SIGNALS_DAY28_AUTO_EXECUTION_ENABLED")):
        return None

    try:
        owner_user_id = UUID(os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip())
        source_ids = _uuid_list(os.getenv("SUPER_SIGNALS_DAY28_SOURCE_IDS", ""))
    except ValueError:
        logger.error("Day 28 automatic execution disabled: invalid owner/source UUID configuration")
        return None

    if not source_ids:
        logger.error("Day 28 automatic execution disabled: source allow-list is empty")
        return None

    broker_key_value = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    broker_keys = tuple(value.strip() for value in broker_key_value.split(",") if value.strip())
    if not broker_keys:
        logger.error("Day 28 automatic execution disabled: broker encryption keys are unavailable")
        return None

    risk_percent = os.getenv("SUPER_SIGNALS_DAY28_RISK_PERCENT", "1").strip() or "1"
    double_lot_approved = _enabled(
        os.getenv("SUPER_SIGNALS_DAY28_ALLOW_DOUBLE_LOT"),
        default=True,
    )

    try:
        cipher = MetaApiTokenCipher(broker_keys)
        read_gateway = MetaApiReadGateway()
        trade_gateway = MetaApiTradeGateway()
        execution = Day28GuardedExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=read_gateway,
            margin_gateway=MetaApiMarginGateway(),
            trade_gateway=trade_gateway,
        )
        management = Day27Mt5ManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=read_gateway,
            trade_gateway=trade_gateway,
        )
        return Day28FullExecutionRouter(
            session_factory=session_factory,
            owner_user_id=owner_user_id,
            execution_service=execution,
            management_service=management,
            allowed_source_ids=source_ids,
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )
    except ValueError as exc:
        logger.error("Day 28 automatic execution disabled: %s", str(exc))
        return None


class Day28TelegramListenerManager(Day21TelegramListenerManager):
    """Day 21 reliable reader plus live-only automatic broker dispatch."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        cipher: TelegramSessionCipher,
        session_factory: sessionmaker[Session],
        refresh_seconds: int = 5,
        excluded_chat_id: int | None = None,
        day28_router: Day28FullExecutionRouter | None = None,
    ) -> None:
        super().__init__(
            api_id=api_id,
            api_hash=api_hash,
            cipher=cipher,
            session_factory=session_factory,
            refresh_seconds=refresh_seconds,
            excluded_chat_id=excluded_chat_id,
        )
        self._session_factory_day28 = session_factory
        self._day28_router = day28_router
        # Serialising canonical dispatch removes concurrent duplicate Telegram
        # delivery races while each account is still executed independently inside
        # the Day 38 router.
        self._day28_dispatch_lock = Lock()

    def _dispatch_sync(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
    ) -> None:
        if self._day28_router is None:
            return
        with self._day28_dispatch_lock:
            try:
                result = asyncio.run(
                    self._day28_router.dispatch_stored_decision(
                        source_id=source_id,
                        telegram_message_id=telegram_message_id,
                        revision_index=revision_index,
                    )
                )
            except Exception:
                logger.exception(
                    "Automatic dispatch failed unexpectedly",
                    extra={
                        "source_id": str(source_id),
                        "telegram_message_id": telegram_message_id,
                        "revision_index": revision_index,
                    },
                )
                return

        if result.outcome == "blocked":
            logger.warning(
                "Broker route blocked code=%s",
                result.error_code or result.reason,
                extra={
                    "source_id": str(source_id),
                    "telegram_message_id": telegram_message_id,
                    "revision_index": revision_index,
                },
            )
        elif result.outcome in {"executed", "managed"}:
            logger.info(
                "Automatic route completed outcome=%s positions=%d broker_actions=%d",
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

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        self._dispatch_sync(
            source_id=captured.source_id,
            telegram_message_id=captured.telegram_message_id,
            revision_index=0,
        )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        if persisted:
            revision_index = self._latest_revision_index(
                captured.source_id,
                captured.telegram_message_id,
            )
            if revision_index > 0:
                self._dispatch_sync(
                    source_id=captured.source_id,
                    telegram_message_id=captured.telegram_message_id,
                    revision_index=revision_index,
                )
        return persisted

    def _latest_revision_index(self, source_id: UUID, telegram_message_id: int) -> int:
        from sqlalchemy import text

        with self._session_factory_day28() as session:
            value = session.execute(
                text(
                    """
                    SELECT COALESCE(MAX(mr.revision_index), 0)
                    FROM messages AS m
                    LEFT JOIN message_revisions AS mr ON mr.message_id = m.id
                    WHERE m.source_id = :source_id
                      AND m.telegram_message_id = :telegram_message_id
                    """
                ),
                {"source_id": source_id, "telegram_message_id": telegram_message_id},
            ).scalar_one_or_none()
        return int(value or 0)

    @staticmethod
    def _fresh_for_live_recovery(value: datetime, *, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        value_utc = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        age_seconds = (reference.astimezone(UTC) - value_utc.astimezone(UTC)).total_seconds()
        return 0 <= age_seconds <= _DAY28_LIVE_RECOVERY_MAX_AGE_SECONDS

    async def _recover_live_gaps(
        self,
        client: TelegramClient,
        plan: ReaderListeningPlan,
    ) -> None:
        """Recover live-push gaps while refusing stale broker replay.

        The connected worker periodically reconciles a small recent Telegram window.
        Newly discovered posts/edits still pass through the normal AI/V1 pipeline.
        Only deliveries no more than 60 seconds old may reach the broker router; older
        recovered history is persisted as evidence only, preserving Day 37 no-replay.
        """
        now = datetime.now(UTC)
        for source in plan.sources:
            messages = await client.get_messages(source.chat_id, limit=_DAY28_LIVE_RECOVERY_LIMIT)
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

                # Call the Day 21 implementation directly so recovery can decide
                # freshness before any broker dispatch occurs.
                inserted = await asyncio.to_thread(
                    Day21TelegramListenerManager._persist_message,
                    self,
                    captured,
                )
                if inserted:
                    if self._fresh_for_live_recovery(posted_at, now=now):
                        await asyncio.to_thread(
                            self._dispatch_sync,
                            source_id=source.source_id,
                            telegram_message_id=int(message_id),
                            revision_index=0,
                        )
                    else:
                        logger.info(
                            "Recovered stale Telegram gap as evidence only",
                            extra={
                                "source_id": str(source.source_id),
                                "telegram_message_id": int(message_id),
                            },
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
                if self._fresh_for_live_recovery(edited_at, now=now):
                    revision_index = await asyncio.to_thread(
                        self._latest_revision_index,
                        source.source_id,
                        int(message_id),
                    )
                    if revision_index > 0:
                        await asyncio.to_thread(
                            self._dispatch_sync,
                            source_id=source.source_id,
                            telegram_message_id=int(message_id),
                            revision_index=revision_index,
                        )
                else:
                    logger.info(
                        "Recovered stale Telegram edit as evidence only",
                        extra={
                            "source_id": str(source.source_id),
                            "telegram_message_id": int(message_id),
                        },
                    )

    async def _catch_up_recent_messages(
        self,
        client: TelegramClient,
        plan: ReaderListeningPlan,
    ) -> None:
        """Run Day 21 catch-up without any broker mutation."""
        for source in plan.sources:
            messages = await client.get_messages(source.chat_id, limit=25)
            for message in reversed(list(messages)):
                message_id = getattr(message, "id", None)
                if message_id is None:
                    continue
                reply_to = getattr(message, "reply_to", None)
                reply_to_message_id = getattr(reply_to, "reply_to_msg_id", None)
                media = getattr(message, "media", None)
                raw_text = str(getattr(message, "raw_text", "") or "")
                captured = CapturedTelegramMessage(
                    source_id=source.source_id,
                    chat_id=source.chat_id,
                    telegram_message_id=int(message_id),
                    raw_text=raw_text,
                    posted_at=self._utc_datetime(getattr(message, "date", None)),
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

                edit_date = getattr(message, "edit_date", None)
                if not inserted and edit_date is not None:
                    captured_edit = CapturedTelegramEdit(
                        source_id=source.source_id,
                        chat_id=source.chat_id,
                        telegram_message_id=int(message_id),
                        raw_text=raw_text,
                        edited_at=self._utc_datetime(edit_date),
                        reply_to_message_id=(
                            int(reply_to_message_id)
                            if reply_to_message_id is not None
                            else None
                        ),
                        has_media=media is not None,
                        media_type=type(media).__name__ if media is not None else None,
                    )
                    await asyncio.to_thread(
                        Day21TelegramListenerManager._persist_edit,
                        self,
                        captured_edit,
                    )


def build_day28_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
    excluded_chat_id: int | None,
) -> Day28TelegramListenerManager:
    # Keep main.py and the proven listener lifecycle untouched. Day 38 supplies only
    # the router beneath it; this import is intentionally local to avoid a module
    # cycle because telegram_listener_day38 reuses this manager class.
    from app.telegram_listener_day38 import build_day38_listener_manager

    return build_day38_listener_manager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
        excluded_chat_id=excluded_chat_id,
    )
