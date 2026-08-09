"""Day 13 Telegram listener extensions for edits, deletions and replies.

The original Day 12 Message row is immutable raw evidence. Edits are appended to
`message_revisions` and audited. Deletions only mark/audit known messages. Reply
identifiers remain in the raw Telegram metadata for later signal follow-up
resolution. Nothing in this module parses signals or creates trade actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.models import AuditEvent, Message, Source
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import (
    CapturedTelegramMessage,
    ReaderListeningPlan,
    TelegramListenerManager,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CapturedTelegramEdit:
    source_id: UUID
    chat_id: int
    telegram_message_id: int
    raw_text: str
    edited_at: datetime
    reply_to_message_id: int | None
    has_media: bool
    media_type: str | None


class Day13TelegramListenerManager(TelegramListenerManager):
    """Extend the Day 12 exact-source listener with non-destructive lifecycle events."""

    async def _run_reader(self, plan: ReaderListeningPlan) -> None:
        session_string = self._cipher.decrypt(plan.session_ciphertext)
        client = TelegramClient(
            StringSession(session_string),
            self._api_id,
            self._api_hash,
            device_model="Super Signals Listener",
            app_version="0.8",
            system_lang_code="en",
            lang_code="en",
        )
        client.session.save_entities = False
        source_by_chat_id = {source.chat_id: source for source in plan.sources}
        exact_chat_ids = tuple(source_by_chat_id)

        async def handle_new_message(event: Any) -> None:
            captured = self._capture_new_message(event, source_by_chat_id)
            if captured is not None:
                await asyncio.to_thread(self._persist_message, captured)

        async def handle_edited_message(event: Any) -> None:
            captured = self._capture_edit(event, source_by_chat_id)
            if captured is not None:
                await asyncio.to_thread(self._persist_edit, captured)

        async def handle_deleted_message(event: Any) -> None:
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None:
                # Telegram does not always provide a chat for deletion updates.
                # Never guess which source a deletion belongs to.
                return
            source = source_by_chat_id.get(int(chat_id))
            if source is None:
                return
            deleted_ids = tuple(
                int(item)
                for item in (getattr(event, "deleted_ids", None) or ())
                if item is not None
            )
            if not deleted_ids:
                return
            await asyncio.to_thread(
                self._persist_deletion,
                source.source_id,
                source.chat_id,
                deleted_ids,
                datetime.now(UTC),
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
            # Deletion updates can omit entity information. Register broadly but
            # hard-filter by exact selected chat id before any persistence.
            client.add_event_handler(handle_deleted_message, events.MessageDeleted())
            logger.info(
                "Telegram reader listening to %d selected source(s)",
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

    @staticmethod
    def _capture_new_message(
        event: Any,
        source_by_chat_id: dict[int, Any],
    ) -> CapturedTelegramMessage | None:
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return None
        source = source_by_chat_id.get(int(chat_id))
        if source is None:
            return None
        message = getattr(event, "message", None)
        message_id = getattr(message, "id", None)
        if message_id is None:
            return None
        posted_at = Day13TelegramListenerManager._utc_datetime(
            getattr(message, "date", None)
        )
        reply_to = getattr(message, "reply_to", None)
        reply_to_message_id = getattr(reply_to, "reply_to_msg_id", None)
        media = getattr(message, "media", None)
        return CapturedTelegramMessage(
            source_id=source.source_id,
            chat_id=source.chat_id,
            telegram_message_id=int(message_id),
            raw_text=str(getattr(event, "raw_text", "") or ""),
            posted_at=posted_at,
            reply_to_message_id=(
                int(reply_to_message_id) if reply_to_message_id is not None else None
            ),
            has_media=media is not None,
            media_type=type(media).__name__ if media is not None else None,
        )

    @staticmethod
    def _capture_edit(
        event: Any,
        source_by_chat_id: dict[int, Any],
    ) -> CapturedTelegramEdit | None:
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return None
        source = source_by_chat_id.get(int(chat_id))
        if source is None:
            return None
        message = getattr(event, "message", None)
        message_id = getattr(message, "id", None)
        if message_id is None:
            return None
        reply_to = getattr(message, "reply_to", None)
        reply_to_message_id = getattr(reply_to, "reply_to_msg_id", None)
        media = getattr(message, "media", None)
        edited_at = Day13TelegramListenerManager._utc_datetime(
            getattr(message, "edit_date", None)
        )
        return CapturedTelegramEdit(
            source_id=source.source_id,
            chat_id=source.chat_id,
            telegram_message_id=int(message_id),
            raw_text=str(getattr(event, "raw_text", "") or ""),
            edited_at=edited_at,
            reply_to_message_id=(
                int(reply_to_message_id) if reply_to_message_id is not None else None
            ),
            has_media=media is not None,
            media_type=type(media).__name__ if media is not None else None,
        )

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        with self._session_factory() as session:
            source = session.scalar(
                select(Source).where(
                    Source.id == captured.source_id,
                    Source.chat_id == captured.chat_id,
                    Source.status.in_({"testing", "live"}),
                )
            )
            if source is None:
                return False

            original = session.scalar(
                select(Message)
                .where(
                    Message.source_id == captured.source_id,
                    Message.telegram_message_id == captured.telegram_message_id,
                )
                .with_for_update()
            )
            if original is None:
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_type="telegram.message_edit_missing_original",
                        entity_type="source",
                        entity_id=captured.source_id,
                        payload={
                            "chat_id": captured.chat_id,
                            "telegram_message_id": captured.telegram_message_id,
                            "edited_at": captured.edited_at.isoformat(),
                            "trade_action_created": False,
                        },
                    )
                )
                session.commit()
                return False

            content_hash = sha256(captured.raw_text.encode("utf-8")).hexdigest()
            latest = session.execute(
                text(
                    """
                    SELECT revision_index, content_sha256, edited_at
                    FROM message_revisions
                    WHERE message_id = :message_id
                    ORDER BY revision_index DESC
                    LIMIT 1
                    """
                ),
                {"message_id": original.id},
            ).mappings().first()
            if (
                latest is not None
                and latest["content_sha256"] == content_hash
                and latest["edited_at"] == captured.edited_at
            ):
                session.rollback()
                return False

            revision_index = 1 if latest is None else int(latest["revision_index"]) + 1
            raw_payload = {
                "chat_id": captured.chat_id,
                "reply_to_message_id": captured.reply_to_message_id,
                "has_media": captured.has_media,
                "media_type": captured.media_type,
            }
            session.execute(
                text(
                    """
                    INSERT INTO message_revisions (
                        message_id,
                        revision_index,
                        raw_text,
                        raw_payload,
                        edited_at,
                        content_sha256
                    )
                    VALUES (
                        :message_id,
                        :revision_index,
                        :raw_text,
                        CAST(:raw_payload AS jsonb),
                        :edited_at,
                        :content_sha256
                    )
                    """
                ),
                {
                    "message_id": original.id,
                    "revision_index": revision_index,
                    "raw_text": captured.raw_text,
                    "raw_payload": json.dumps(raw_payload),
                    "edited_at": captured.edited_at,
                    "content_sha256": content_hash,
                },
            )
            original.edited_at = captured.edited_at
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="telegram.message_edited",
                    entity_type="message",
                    entity_id=original.id,
                    payload={
                        "source_id": str(captured.source_id),
                        "chat_id": captured.chat_id,
                        "telegram_message_id": captured.telegram_message_id,
                        "revision_index": revision_index,
                        "reply_to_message_id": captured.reply_to_message_id,
                        "edited_at": captured.edited_at.isoformat(),
                        "original_preserved": True,
                        "trade_action_created": False,
                    },
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
            return True

    def _persist_deletion(
        self,
        source_id: UUID,
        chat_id: int,
        telegram_message_ids: tuple[int, ...],
        deleted_at: datetime,
    ) -> int:
        with self._session_factory() as session:
            source = session.scalar(
                select(Source).where(
                    Source.id == source_id,
                    Source.chat_id == chat_id,
                    Source.status.in_({"testing", "live"}),
                )
            )
            if source is None:
                return 0

            known_messages = session.scalars(
                select(Message).where(
                    Message.source_id == source_id,
                    Message.telegram_message_id.in_(telegram_message_ids),
                )
            ).all()
            newly_deleted: list[Message] = []
            for message in known_messages:
                if message.deleted_at is None:
                    message.deleted_at = deleted_at
                    newly_deleted.append(message)

            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="telegram.messages_deleted",
                    entity_type="source",
                    entity_id=source_id,
                    payload={
                        "chat_id": chat_id,
                        "telegram_message_ids": list(telegram_message_ids),
                        "matched_message_ids": [str(item.id) for item in known_messages],
                        "newly_marked_deleted": len(newly_deleted),
                        "deleted_at": deleted_at.isoformat(),
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
            return len(newly_deleted)

    @staticmethod
    def _utc_datetime(value: Any) -> datetime:
        if not isinstance(value, datetime):
            return datetime.now(UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def build_day13_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
) -> Day13TelegramListenerManager:
    return Day13TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
    )
