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
    shadow_total: int
    shadow_open: int
    shadow_closed: int
    shadow_wins: int
    shadow_losses: int
    shadow_return_percent: str


class Day14TelegramSourceService(TelegramSourceService):
    """Keep logical sources durable while reporting private-reader availability."""

    def list_shared_sources(self, session: Session) -> list[Day14SharedTelegramSourceView]:
        # Keep the Day 14 connectivity view compatible with the current shared-source
        # API contract.  The base service owns the shadow-trading metric semantics;
        # reusing it here prevents this reliability override from returning an older
        # object shape when new shared-source fields are added.
        shared_metrics = {
            item.source_id: item for item in super().list_shared_sources(session)
        }
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
        result: list[Day14SharedTelegramSourceView] = []
        for row in rows:
            source_id = UUID(str(row["source_id"]))
            metrics = shared_metrics[source_id]
            connected_reader_count = int(row["connected_reader_count"] or 0)
            result.append(
                Day14SharedTelegramSourceView(
                    source_id=source_id,
                    chat_id=int(row["chat_id"]),
                    title=str(row["title"]),
                    status=str(row["status"]),
                    connection_status=(
                        "connected" if connected_reader_count > 0 else "disconnected"
                    ),
                    connected_reader_count=connected_reader_count,
                    shadow_total=metrics.shadow_total,
                    shadow_open=metrics.shadow_open,
                    shadow_closed=metrics.shadow_closed,
                    shadow_wins=metrics.shadow_wins,
                    shadow_losses=metrics.shadow_losses,
                    shadow_return_percent=metrics.shadow_return_percent,
                )
            )
        return result

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
