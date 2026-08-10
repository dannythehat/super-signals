"""Day 14 listener reliability extensions.

Day 13 lifecycle capture remains intact. Day 14 adds one reliability rule: when
Telegram proves that a saved reader session is no longer authorised (or its
stored encrypted session cannot be recovered), that reader is marked revoked
and its saved server session is destroyed. Shared sources and reader links are
not deleted, allowing the existing planner to use another connected fallback
reader or report the logical source as disconnected.

A lightweight reconciliation loop also re-reads the latest known Telegram
message IDs for each exact selected source. This closes the small race where a
MessageEdited event can be missed while the Telegram connection is briefly
between updates. Recovery is limited to message IDs already present in the
canonical Message log, so it cannot broaden ingestion to unrelated chats.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.models import AuditEvent, Message, TelegramAccount
from app.telegram_crypto import SessionDecryptionError, TelegramSessionCipher
from app.telegram_listener import ReaderListeningPlan
from app.telegram_listener_day13 import (
    CapturedTelegramEdit,
    Day13TelegramListenerManager,
)
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class Day14TelegramListenerManager(Day13TelegramListenerManager):
    """Day 13 listener plus safe access-loss and missed-edit recovery."""

    async def _run_reader(self, plan: ReaderListeningPlan) -> None:
        try:
            session_string = self._cipher.decrypt(plan.session_ciphertext)
        except SessionDecryptionError:
            await asyncio.to_thread(
                self._mark_reader_unavailable,
                plan.telegram_account_id,
                "session_decryption_failed",
            )
            return

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
        recovery_task: asyncio.Task[None] | None = None

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
                await asyncio.to_thread(
                    self._mark_reader_unavailable,
                    plan.telegram_account_id,
                    "telegram_not_authorized",
                )
                logger.warning(
                    "Telegram reader access was revoked",
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
            logger.info(
                "Telegram reader listening to %d selected source(s)",
                len(exact_chat_ids),
                extra={"telegram_account_id": str(plan.telegram_account_id)},
            )

            # Telegram's real-time edit update can occasionally be missed during a
            # brief transport/reconnect boundary. Reconcile only already-known
            # message IDs from exact selected sources. The first pass runs
            # immediately, then repeats at a deliberately low frequency.
            recovery_task = asyncio.create_task(
                self._recover_known_edits_loop(client, source_by_chat_id),
                name=f"super-signals-telegram-edit-recovery-{plan.telegram_account_id}",
            )
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Network/transient Telegram failures are retried by reconciliation.
            # Do not mark a reader revoked unless Telegram actually proves the
            # session is unavailable.
            logger.exception(
                "Telegram reader worker failed",
                extra={"telegram_account_id": str(plan.telegram_account_id)},
            )
        finally:
            if recovery_task is not None:
                recovery_task.cancel()
                await self._await_cancelled(recovery_task)
            if client.is_connected():
                await client.disconnect()

    async def _recover_known_edits_loop(
        self,
        client: TelegramClient,
        source_by_chat_id: dict[int, Any],
    ) -> None:
        interval_seconds = max(30, self._refresh_seconds * 6)
        while True:
            try:
                await self._recover_known_edits_once(client, source_by_chat_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Recovery is defence in depth. A failed pass must never stop the
                # primary real-time Telegram listener.
                logger.exception("Telegram missed-edit recovery pass failed")
            await asyncio.sleep(interval_seconds)

    async def _recover_known_edits_once(
        self,
        client: TelegramClient,
        source_by_chat_id: dict[int, Any],
    ) -> None:
        for source in source_by_chat_id.values():
            known_ids = await asyncio.to_thread(
                self._load_recent_known_message_ids,
                source.source_id,
            )
            if not known_ids:
                continue

            # Supplying explicit IDs keeps this recovery strictly bound to
            # canonical messages we already ingested from this exact source.
            recovered_messages = await client.get_messages(
                source.chat_id,
                ids=list(known_ids),
            )
            if recovered_messages is None:
                continue
            if not isinstance(recovered_messages, (list, tuple)):
                recovered_messages = [recovered_messages]

            for message in recovered_messages:
                captured = self._capture_recovered_edit(source, message)
                if captured is None:
                    continue
                needs_recovery = await asyncio.to_thread(
                    self._edit_recovery_needed,
                    captured,
                )
                if not needs_recovery:
                    continue
                await asyncio.to_thread(self._persist_edit, captured)

    def _load_recent_known_message_ids(
        self,
        source_id: UUID,
        limit: int = 25,
    ) -> tuple[int, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(Message.telegram_message_id)
                .where(
                    Message.source_id == source_id,
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.posted_at.desc(), Message.telegram_message_id.desc())
                .limit(limit)
            ).all()
        return tuple(int(item) for item in rows)

    @staticmethod
    def _capture_recovered_edit(
        source: Any,
        message: Any,
    ) -> CapturedTelegramEdit | None:
        if message is None:
            return None
        message_id = getattr(message, "id", None)
        edit_date = getattr(message, "edit_date", None)
        if message_id is None or edit_date is None:
            return None

        reply_to = getattr(message, "reply_to", None)
        reply_to_message_id = getattr(reply_to, "reply_to_msg_id", None)
        media = getattr(message, "media", None)
        raw_text = str(
            getattr(message, "raw_text", None)
            or getattr(message, "message", "")
            or ""
        )
        return CapturedTelegramEdit(
            source_id=source.source_id,
            chat_id=source.chat_id,
            telegram_message_id=int(message_id),
            raw_text=raw_text,
            edited_at=Day14TelegramListenerManager._utc_datetime(edit_date),
            reply_to_message_id=(
                int(reply_to_message_id) if reply_to_message_id is not None else None
            ),
            has_media=media is not None,
            media_type=type(media).__name__ if media is not None else None,
        )

    def _edit_recovery_needed(self, captured: CapturedTelegramEdit) -> bool:
        content_hash = sha256(captured.raw_text.encode("utf-8")).hexdigest()
        with self._session_factory() as session:
            original = session.scalar(
                select(Message).where(
                    Message.source_id == captured.source_id,
                    Message.telegram_message_id == captured.telegram_message_id,
                )
            )
            if original is None or original.deleted_at is not None:
                return False

            latest = session.execute(
                text(
                    """
                    SELECT content_sha256, edited_at
                    FROM message_revisions
                    WHERE message_id = :message_id
                    ORDER BY revision_index DESC
                    LIMIT 1
                    """
                ),
                {"message_id": original.id},
            ).mappings().first()
            if latest is None:
                return True

            latest_edited_at = self._utc_datetime(latest["edited_at"])
            if latest_edited_at > captured.edited_at:
                return False
            if (
                latest_edited_at == captured.edited_at
                and latest["content_sha256"] == content_hash
            ):
                return False
            return True

    def _mark_reader_unavailable(self, telegram_account_id: UUID, reason: str) -> bool:
        with self._session_factory() as session:
            account = session.get(TelegramAccount, telegram_account_id)
            if account is None or account.status != "connected":
                return False

            marker = f"destroyed:{uuid4()}"
            account.session_ciphertext = self._cipher.encrypt(marker)
            account.session_fingerprint = self._cipher.fingerprint(marker)
            account.status = "revoked"
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="telegram.reader_access_lost",
                    entity_type="telegram_account",
                    entity_id=account.id,
                    payload={
                        "reason": reason,
                        "server_session_destroyed": True,
                        "source_links_preserved": True,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
            return True


def build_day14_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
) -> Day14TelegramListenerManager:
    return Day14TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
    )
