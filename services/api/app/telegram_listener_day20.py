"""Day 20 listener: attach provider updates to canonical Signals.

When AI supervision is enabled, legacy deterministic lifecycle interpretation stays
available only as fallback evidence and cannot act before the AI decision.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.signal_lifecycle import SignalLifecycleService
from app.standalone_lifecycle_linker_v2 import StandaloneLifecycleLinkerV2
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
        self._standalone_lifecycle = StandaloneLifecycleLinkerV2(session_factory)
        self._ai_supervisor_enabled = get_settings().ai_supervisor_enabled

    async def start(self) -> None:
        await super().start()
        if not self._ai_supervisor_enabled:
            await asyncio.to_thread(self._standalone_lifecycle.recover_recent)
            await asyncio.to_thread(self._lifecycle_service.backfill)

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        persisted = super()._persist_message(captured)
        if not self._ai_supervisor_enabled:
            handled_as_standalone = self._standalone_lifecycle.process_original(
                captured.source_id,
                captured.telegram_message_id,
            )
            if not handled_as_standalone:
                self._lifecycle_service.process_original(
                    captured.source_id,
                    captured.telegram_message_id,
                )
        return persisted

    def _persist_edit(self, captured: CapturedTelegramEdit) -> bool:
        persisted = super()._persist_edit(captured)
        if not self._ai_supervisor_enabled:
            handled_as_standalone = self._standalone_lifecycle.process_latest_revision(
                captured.source_id,
                captured.telegram_message_id,
            )
            if not handled_as_standalone:
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
