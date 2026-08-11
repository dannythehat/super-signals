"""Day 17 Telegram listener with strict validation evidence.

Historical Day 17 review rows remain intact. When the AI Message Supervisor is
enabled, new messages do not enter a human review queue because the live path must
make an immediate automatic decision.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.message_review import MessageReviewService
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day16 import Day16TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker


class Day17TelegramListenerManager(Day16TelegramListenerManager):
    """Day 16 listener plus legacy Day 17 validation/review processing."""

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
        self._ai_supervisor_enabled = get_settings().ai_supervisor_enabled

    async def start(self) -> None:
        await super().start()
        if not self._ai_supervisor_enabled:
            await asyncio.to_thread(self._review_service.backfill)

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        if not self._ai_supervisor_enabled:
            self._review_service.process_original(
                captured.source_id,
                captured.telegram_message_id,
            )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        if not self._ai_supervisor_enabled:
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
