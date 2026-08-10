"""Day 16 Telegram listener with first XAUUSD parser stage.

Day 15 classification remains authoritative. Only New Trade / classified evidence
is offered to the parser. Parser output is append-only evidence and never creates
Signals, Positions, lot sizing or broker actions.
"""

from __future__ import annotations

import asyncio

from app.message_parser import MessageParserService
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day15 import Day15TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker


class Day16TelegramListenerManager(Day15TelegramListenerManager):
    """Day 15 listener plus append-only XAUUSD parser evidence."""

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
        self._parser_service = MessageParserService(session_factory)

    async def start(self) -> None:
        # Day 15 first restores any missing classifications. Once that accepted
        # stage is live, Day 16 can safely backfill only eligible new trades.
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
