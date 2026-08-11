"""Day 16 Telegram listener with versioned real-source XAUUSD/GOLD parsing."""

from __future__ import annotations

import asyncio

from app.message_parser_v2 import MessageParserServiceV2
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day15 import Day15TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker


class Day16TelegramListenerManager(Day15TelegramListenerManager):
    """Day 15 listener plus append-only parser-v2 evidence for new messages."""

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
        self._parser_service = MessageParserServiceV2(session_factory)

    async def start(self) -> None:
        await super().start()
        await asyncio.to_thread(self._parser_service.backfill_eligible)

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        self._parser_service.parse_original(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        self._parser_service.parse_latest_revision(
            captured.source_id,
            captured.telegram_message_id,
        )
        return persisted


def build_day16_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
) -> Day16TelegramListenerManager:
    return Day16TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
    )
