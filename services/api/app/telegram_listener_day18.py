"""Day 18 Telegram listener with canonical Signal creation and deduplication.

When AI supervision is enabled, the deterministic parser remains evidence only and
cannot promote a message into a canonical Signal before the AI decision arrives.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.signal_events import CanonicalSignalService
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day17 import Day17TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker


class Day18TelegramListenerManager(Day17TelegramListenerManager):
    """Day 17 listener plus idempotent canonical Signal creation."""

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
        self._signal_service = CanonicalSignalService(session_factory)
        self._ai_supervisor_enabled = get_settings().ai_supervisor_enabled

    async def start(self) -> None:
        await super().start()
        if not self._ai_supervisor_enabled:
            await asyncio.to_thread(self._signal_service.backfill)

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        if not self._ai_supervisor_enabled:
            self._signal_service.process_original(
                captured.source_id,
                captured.telegram_message_id,
            )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        if not self._ai_supervisor_enabled:
            self._signal_service.process_original(
                captured.source_id,
                captured.telegram_message_id,
            )
        return persisted


def build_day18_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
) -> Day18TelegramListenerManager:
    return Day18TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
    )
