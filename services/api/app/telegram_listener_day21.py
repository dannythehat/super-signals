"""Day 21 Telegram listener reliability gate support.

Adds a bounded, source-scoped catch-up on reader start/reconnect so short Render
cutovers cannot silently lose Telegram messages. Catch-up reuses the accepted
idempotent Day 20 persistence pipeline and never broadens the selected source set.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage, ReaderListeningPlan
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day20 import Day20TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)
_DAY21_CATCHUP_LIMIT = 25


class Day21TelegramListenerManager(Day20TelegramListenerManager):
    """Day 20 pipeline plus bounded reconnect catch-up for exact selected sources."""

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

            # Register live handlers first, then close the small deployment gap.
            # Existing source/message uniqueness and lifecycle event keys make
            # overlap between live delivery and catch-up harmless.
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

                # If this message was already known before the reconnect, a fetched
                # current Telegram edit can safely append a missing revision. If the
                # message itself was missed and is already edited, we preserve the
                # current Telegram text as newly recovered evidence but do not invent
                # a historical pre-edit version that Telegram no longer provides.
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
