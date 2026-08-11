"""Day 15 Telegram listener with conservative message classification.

All Day 14 reliability/privacy behaviour remains intact. New messages use the
versioned real-source classifier correction while historical Day 15 evidence is
left untouched.
"""

from __future__ import annotations

import asyncio

from app.message_classifier_v2 import MessageClassificationServiceV2
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
        self._classification_service = MessageClassificationServiceV2(session_factory)

    async def start(self) -> None:
        await asyncio.to_thread(self._classification_service.backfill_unclassified)
        await super().start()

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        self._classification_service.classify_original(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
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
