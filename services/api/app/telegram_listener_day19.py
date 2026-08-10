"""Day 19 listener cutover support for the shared Super Signals Telegram chat."""

from __future__ import annotations

from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import ReaderListeningPlan
from app.telegram_listener_day18 import Day18TelegramListenerManager
from sqlalchemy.orm import Session, sessionmaker


class Day19TelegramListenerManager(Day18TelegramListenerManager):
    """Day 18 listener with one explicit publish-destination exclusion.

    During Day 19 cutover the existing Super Signals chat can remain recorded as a
    Testing source for historical/admin continuity while being excluded from the
    private-reader listening plan. This prevents the publish-only bot from feeding
    its own output back into ingestion.
    """

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
        )
        self._excluded_chat_id = excluded_chat_id

    def _load_plan(self) -> dict:
        plan = super()._load_plan()
        if self._excluded_chat_id is None:
            return plan

        filtered: dict = {}
        for reader_id, reader_plan in plan.items():
            sources = tuple(
                source
                for source in reader_plan.sources
                if source.chat_id != self._excluded_chat_id
            )
            if not sources:
                continue
            filtered[reader_id] = ReaderListeningPlan(
                telegram_account_id=reader_plan.telegram_account_id,
                session_ciphertext=reader_plan.session_ciphertext,
                sources=sources,
            )
        return filtered


def build_day19_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
    excluded_chat_id: int | None,
) -> Day19TelegramListenerManager:
    return Day19TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
        excluded_chat_id=excluded_chat_id,
    )
