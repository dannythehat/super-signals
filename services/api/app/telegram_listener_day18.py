"""Day 18 Telegram listener with canonical Signal creation and deduplication."""

from __future__ import annotations

import asyncio

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

    async def start(self) -> None:
        # Accepted classification, parsing and validation backfills run first.
        # Day 18 then creates only revision-0 Signals that already passed all gates.
        await super().start()
        await asyncio.to_thread(self._signal_service.backfill)

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        self._signal_service.process_original(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        # An edit is a revision of the same provider message, never a second Signal.
        # Re-checking revision 0 is intentionally idempotent and does not promote a
        # previously stopped message merely because a later revision changed it.
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
