"""Day 21 Telegram listener reliability plus AI-authoritative message decisions.

Every newly persisted Testing/Live source message and edit is immediately passed to
the AI Message Supervisor when enabled. Reconnect catch-up uses the same idempotent
pipeline, so a Render cutover cannot bypass supervision.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.ai_message_pipeline import AiMessagePipeline
from app.ai_message_supervisor_day34 import Day34OpenAiMessageSupervisor
from app.ai_source_aware_pipeline import SourceAwareAiMessagePipeline
from app.ai_supervisor_acceptance import run_ai_supervisor_acceptance_probe
from app.config import get_settings
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage, ReaderListeningPlan
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day20 import Day20TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)
_DAY21_CATCHUP_LIMIT = 25


class Day21TelegramListenerManager(Day20TelegramListenerManager):
    """Day 20 pipeline plus catch-up and immediate AI message supervision."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        cipher: TelegramSessionCipher,
        session_factory: sessionmaker[Session],
        refresh_seconds: int = 5,
        excluded_chat_id: int | None = None,
    ) -> None:
        super().__init__(
            api_id=api_id,
            api_hash=api_hash,
            cipher=cipher,
            session_factory=session_factory,
            refresh_seconds=refresh_seconds,
            excluded_chat_id=excluded_chat_id,
        )
        settings = get_settings()
        if os.getenv("SUPER_SIGNALS_AI_ACCEPTANCE_PROBE", "").strip() == "1":
            run_ai_supervisor_acceptance_probe(settings)

        self._ai_pipeline: AiMessagePipeline | None = None
        if settings.ai_supervisor_enabled:
            supervisor = None
            if settings.ai_supervisor_api_key:
                supervisor = Day34OpenAiMessageSupervisor(
                    api_key=settings.ai_supervisor_api_key,
                    model=settings.ai_supervisor_model,
                    timeout_seconds=settings.ai_supervisor_timeout_seconds,
                )
            self._ai_pipeline = SourceAwareAiMessagePipeline(
                session_factory=session_factory,
                supervisor=supervisor,
            )

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        if self._ai_pipeline is not None:
            self._ai_pipeline.process_original(
                captured.source_id,
                captured.telegram_message_id,
            )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        if persisted and self._ai_pipeline is not None:
            self._ai_pipeline.process_latest_revision(
                captured.source_id,
                captured.telegram_message_id,
            )
        return persisted

    async def _run_reader(self, plan: ReaderListeningPlan) -> None:
        session_string = self._cipher.decrypt(plan.session_ciphertext)
        client = TelegramClient(
            StringSession(session_string),
            self._api_id,
            self._api_hash,
            device_model="Super Signals Listener",
            app_version="0.9",
            system_lang_code="en",
            lang_code="en",
        )
        client.session.save_entities = False
        source_by_chat_id = {source.chat_id: source for source in plan.sources}
        exact_chat_ids = tuple(source_by_chat_id)
        selected_source_ids = tuple(source.source_id for source in plan.sources)

        async def handle_new_message(event: Any) -> None:
            captured = self._capture_new_message(event, source_by_chat_id)
            if captured is not None:
                await asyncio.to_thread(self._persist_message, captured)

        async def handle_edited_message(event: Any) -> None:
            captured = self._capture_edit(event, source_by_chat_id)
            if captured is not None:
                await asyncio.to_thread(self._persist_edit, captured)

        async def handle_deleted_message(event: Any) -> None:
            deleted_ids = tuple(
                int(item)
                for item in (getattr(event, "deleted_ids", None) or ())
                if item is not None
            )
            if not deleted_ids:
                return

            from datetime import UTC, datetime

            deleted_at = datetime.now(UTC)
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None:
                targets = await asyncio.to_thread(
                    self._resolve_chatless_deletions,
                    selected_source_ids,
                    deleted_ids,
                )
                for source_id, resolved_chat_id, resolved_ids in targets:
                    await asyncio.to_thread(
                        self._persist_deletion,
                        source_id,
                        resolved_chat_id,
                        resolved_ids,
                        deleted_at,
                    )
                return

            source = source_by_chat_id.get(int(chat_id))
            if source is None:
                return
            await asyncio.to_thread(
                self._persist_deletion,
                source.source_id,
                source.chat_id,
                deleted_ids,
                deleted_at,
            )

        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(
                    "Telegram reader is no longer authorised",
                    extra={"telegram_account_id": str(plan.telegram_account_id)},
                )
                return

            client.add_event_handler(
                handle_new_message,
                events.NewMessage(chats=list(exact_chat_ids)),
            )
            client.add_event_handler(
                handle_edited_message,
                events.MessageEdited(chats=list(exact_chat_ids)),
            )
            client.add_event_handler(handle_deleted_message, events.MessageDeleted())

            await self._catch_up_recent_messages(client, plan)

            logger.info(
                "Telegram reader listening to %d selected source(s) after bounded catch-up",
                len(exact_chat_ids),
                extra={"telegram_account_id": str(plan.telegram_account_id)},
            )
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Telegram reader worker failed",
                extra={"telegram_account_id": str(plan.telegram_account_id)},
            )
        finally:
            if client.is_connected():
                await client.disconnect()

    async def _catch_up_recent_messages(
        self,
        client: TelegramClient,
        plan: ReaderListeningPlan,
    ) -> None:
        for source in plan.sources:
            messages = await client.get_messages(source.chat_id, limit=_DAY21_CATCHUP_LIMIT)
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
                        int(reply_to_message_id)
                        if reply_to_message_id is not None
                        else None
                    ),
                    has_media=media is not None,
                    media_type=type(media).__name__ if media is not None else None,
                )
                inserted = await asyncio.to_thread(self._persist_message, captured)

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
                    await asyncio.to_thread(self._persist_edit, captured_edit)


def build_day21_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
    excluded_chat_id: int | None,
) -> Day21TelegramListenerManager:
    return Day21TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
        excluded_chat_id=excluded_chat_id,
    )
