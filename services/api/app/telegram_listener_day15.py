"""Day 15 Telegram listener with conservative message classification.

All Day 14 reliability/privacy behaviour remains intact. Day 15 classifies raw
message evidence after it has been safely persisted. It does not parse Signals,
create Positions or perform any trade action.
"""

from __future__ import annotations

import asyncio

from app.message_classifier import MessageClassificationService
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day14 import Day14TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker


class Day15TelegramListenerManager(Day14TelegramListenerManager):
    """Day 14 listener plus append-only classification evidence."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        cipher: TelegramSessionCipher,
        session_factory: sessionmaker[Session],
        refresh_seconds: int = 5,
    ) -> None:
        super().__init__(
            api_id=api_id,
            api_hash=api_hash,
            cipher=cipher,
            session_factory=session_factory,
            refresh_seconds=refresh_seconds,
        )
        self._classification_service = MessageClassificationService(session_factory)

    async def start(self) -> None:
        # A restart must not strand raw messages that were committed immediately
        # before the process stopped. Backfill is idempotent because the table is
        # unique on (message_id, revision_index).
        await asyncio.to_thread(self._classification_service.backfill_unclassified)
        await super().start()

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        # Ensure classification even if Telegram replays a message whose raw row
        # was already committed but whose classification was interrupted.
        self._classification_service.classify_original(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        # Classify the latest append-only revision. Earlier classification rows
        # are never overwritten.
        self._classification_service.classify_latest_revision(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted


def build_day15_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
) -> Day15TelegramListenerManager:
    return Day15TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
    )
