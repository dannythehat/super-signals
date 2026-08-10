"""Day 17 Telegram listener with strict validation and admin review evidence."""

from __future__ import annotations

import asyncio

from app.message_review import MessageReviewService
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day16 import Day16TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker


class Day17TelegramListenerManager(Day16TelegramListenerManager):
    """Day 16 listener plus strict validation/review queue processing."""

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
        self._review_service = MessageReviewService(session_factory)

    async def start(self) -> None:
        # Accepted Day 15/16 stages backfill first. Day 17 then evaluates every
        # existing revision deterministically and idempotently.
        await super().start()
        await asyncio.to_thread(self._review_service.backfill)

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        self._review_service.process_original(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        self._review_service.process_latest_revision(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted


def build_day17_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
) -> Day17TelegramListenerManager:
    return Day17TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
    )
