"""Day 14 listener reliability extensions.

Day 13 lifecycle capture remains intact. Day 14 adds one reliability rule: when
Telegram proves that a saved reader session is no longer authorised (or its
stored encrypted session cannot be recovered), that reader is marked revoked
and its saved server session is destroyed. Shared sources and reader links are
not deleted, allowing the existing planner to use another connected fallback
reader or report the logical source as disconnected.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.models import AuditEvent, TelegramAccount
from app.telegram_crypto import SessionDecryptionError, TelegramSessionCipher
from app.telegram_listener import ReaderListeningPlan
from app.telegram_listener_day13 import Day13TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class Day14TelegramListenerManager(Day13TelegramListenerManager):
    """Day 13 lifecycle listener plus safe reader-access-loss detection."""

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
            if client.is_connected():
                await client.disconnect()

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
