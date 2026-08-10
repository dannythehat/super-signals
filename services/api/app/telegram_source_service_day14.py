"""Day 14 Telegram source reliability and connectivity semantics.

Operating state (Testing/Live/Paused) is deliberately separate from reader
connectivity. A logical shared source remains in the catalogue when its last
private reader link disappears; it is reported as DISCONNECTED until a valid
connected reader is linked again.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Source
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_source_gateway import TelethonTelegramSourceGateway
from app.telegram_source_service import (
    TelegramSourceConfigurationError,
    TelegramSourceNotFoundError,
    TelegramSourceService,
)


@dataclass(frozen=True, slots=True)
class Day14SharedTelegramSourceView:
    source_id: UUID
    chat_id: int
    title: str
    status: str
    connection_status: str
    connected_reader_count: int


class Day14TelegramSourceService(TelegramSourceService):
    """Keep logical sources durable while reporting private-reader availability."""

    def list_shared_sources(self, session: Session) -> list[Day14SharedTelegramSourceView]:
        rows = session.execute(
            text(
                """
                SELECT
                    s.id AS source_id,
                    s.chat_id,
                    COALESCE(s.chat_title, s.source_alias) AS title,
                    s.status,
                    COUNT(ta.id) FILTER (WHERE ta.status = 'connected') AS connected_reader_count
                FROM sources AS s
                LEFT JOIN source_reader_access AS sra
                  ON sra.source_id = s.id
                LEFT JOIN telegram_accounts AS ta
                  ON ta.id = sra.telegram_account_id
                WHERE s.status != 'revoked'
                GROUP BY s.id, s.chat_id, s.chat_title, s.source_alias, s.status, s.created_at
                ORDER BY s.created_at ASC, s.id ASC
                """
            )
        ).mappings().all()
        return [
            Day14SharedTelegramSourceView(
                source_id=row["source_id"],
                chat_id=int(row["chat_id"]),
                title=str(row["title"]),
                status=str(row["status"]),
                connection_status=(
                    "connected" if int(row["connected_reader_count"] or 0) > 0 else "disconnected"
                ),
                connected_reader_count=int(row["connected_reader_count"] or 0),
            )
            for row in rows
        ]

    def unselect_source(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        account_id: UUID,
        source_id: UUID,
    ) -> dict[str, Any]:
        account = self._account_for_actor(session, actor, account_id)
        source = session.get(Source, source_id)
        if source is None or source.status == "revoked":
            raise TelegramSourceNotFoundError("Selected Telegram source was not found.")
        if source_id not in self._reader_source_ids(session, account.id):
            raise TelegramSourceNotFoundError(
                "This reader does not have an access link to that Telegram source."
            )

        session.execute(
            text(
                """
                DELETE FROM source_reader_access
                WHERE source_id = :source_id
                  AND telegram_account_id = :telegram_account_id
                """
            ),
            {"source_id": source.id, "telegram_account_id": account.id},
        )

        remaining_reader_ids = self._source_reader_ids(session, source.id)
        if remaining_reader_ids and source.telegram_account_id == account.id:
            source.telegram_account_id = remaining_reader_ids[0]

        connected_reader_count = int(
            session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM source_reader_access AS sra
                    JOIN telegram_accounts AS ta
                      ON ta.id = sra.telegram_account_id
                    WHERE sra.source_id = :source_id
                      AND ta.status = 'connected'
                    """
                ),
                {"source_id": source.id},
            ).scalar_one()
        )
        source_disconnected = connected_reader_count == 0

        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.source_reader_removed",
            source_id=source.id,
            payload={
                "telegram_account_id": str(account.id),
                "chat_id": source.chat_id,
                "source_revoked": False,
                "source_disconnected": source_disconnected,
                "remaining_reader_count": len(remaining_reader_ids),
                "connected_reader_count": connected_reader_count,
                "monitoring_started": False,
            },
        )
        session.commit()
        return {
            "removed": True,
            "source_id": source.id,
            "monitoring_started": False,
        }


@lru_cache
def get_day14_telegram_source_service() -> Day14TelegramSourceService:
    settings = get_settings()
    if settings.telegram_api_id is None or settings.telegram_api_hash is None:
        raise TelegramSourceConfigurationError(
            "Telegram source discovery is not configured on this server."
        )
    return Day14TelegramSourceService(
        gateway=TelethonTelegramSourceGateway(
            settings.telegram_api_id,
            settings.telegram_api_hash,
        ),
        cipher=TelegramSessionCipher(settings.telegram_session_keys),
    )
