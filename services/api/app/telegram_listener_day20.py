"""Day 20 listener: attach explicit provider updates to canonical Signals."""

from __future__ import annotations

import asyncio

from app.signal_lifecycle import SignalLifecycleService
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day19 import Day19TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker


class Day20TelegramListenerManager(Day19TelegramListenerManager):
    """Day 19 listener plus safe, idempotent lifecycle-event linking."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        cipher: TelegramSessionCipher,
        session_factory: sessionmaker[Session],
        refresh_seconds: int = 5,
        excluded_chat_id: int | None = None,
    ) -> None:
        super().__init__(
            api_id=api_id,
            api_hash=api_hash,
            cipher=cipher,
            session_factory=session_factory,
            refresh_seconds=refresh_seconds,
            excluded_chat_id=excluded_chat_id,
        )
        self._lifecycle_service = SignalLifecycleService(session_factory)

    async def start(self) -> None:
        await super().start()
        await asyncio.to_thread(self._lifecycle_service.backfill)

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        self._lifecycle_service.process_original(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        self._lifecycle_service.process_latest_revision(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted


def build_day20_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
    excluded_chat_id: int | None,
) -> Day20TelegramListenerManager:
    return Day20TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
        excluded_chat_id=excluded_chat_id,
    )
