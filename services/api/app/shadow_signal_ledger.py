"""Canonical signal ledger that admits isolated Provider Lab shadow sources.

The production semantic pipeline deliberately allows ``shadow`` sources so their
provider instructions can be studied. The ordinary canonical signal service still
protects broker execution by accepting only testing/live sources, so Provider Lab needs
an explicit ledger boundary that also admits shadow rows. Testing/live canonical signals
are also mirrored into the fixed-dollar research benchmark without changing broker routing.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.canonical_signal_ledger import CanonicalSignalLedger
from app.shadow_trading import ShadowTradeService

logger = logging.getLogger(__name__)


class ShadowAwareCanonicalSignalLedger(CanonicalSignalLedger):
    """Create canonical evidence and mirror testing/live signals into Provider Lab."""

    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self._benchmark_shadow = ShadowTradeService(session_factory)

    def process(
        self,
        *,
        message_id: UUID,
        extracted: dict[str, Any],
        revision_index: int = 0,
    ):
        result = super().process(
            message_id=message_id,
            extracted=extracted,
            revision_index=revision_index,
        )
        if result.created and result.signal_id is not None:
            self._mirror_testing_live_signal(message_id, result.signal_id)
        return result

    def _mirror_testing_live_signal(self, message_id: UUID, signal_id: UUID) -> None:
        """Research failure never blocks or mutates canonical broker execution."""
        try:
            with self._session_factory() as session:
                status = session.execute(
                    text(
                        """
                        SELECT s.status FROM messages m
                        JOIN sources s ON s.id=m.source_id
                        WHERE m.id=:message_id LIMIT 1
                        """
                    ),
                    {"message_id": message_id},
                ).scalar_one_or_none()
            if str(status or "") not in {"testing", "live"}:
                return
            self._benchmark_shadow.record_signal(signal_id)
        except Exception:
            logger.exception(
                "Provider Lab benchmark mirror failed safely",
                extra={"message_id": str(message_id), "signal_id": str(signal_id)},
            )

    @staticmethod
    def _message_revision_row(
        session: Session,
        message_id: UUID,
        revision_index: int,
    ) -> Any | None:
        return session.execute(
            text(
                """
                SELECT
                    m.id AS message_id,
                    m.source_id,
                    s.chat_id AS provider_chat_id,
                    m.telegram_message_id AS provider_message_id,
                    CASE WHEN :revision_index = 0 THEN m.posted_at ELSE mr.edited_at END AS source_posted_at,
                    CASE WHEN :revision_index = 0 THEN m.raw_text ELSE mr.raw_text END AS original_text
                FROM messages AS m
                JOIN sources AS s ON s.id = m.source_id
                LEFT JOIN message_revisions AS mr
                  ON mr.message_id = m.id
                 AND mr.revision_index = :revision_index
                WHERE m.id = :message_id
                  AND m.deleted_at IS NULL
                  AND s.status IN ('testing', 'shadow', 'live')
                  AND (:revision_index = 0 OR mr.revision_index IS NOT NULL)
                """
            ),
            {"message_id": message_id, "revision_index": revision_index},
        ).mappings().first()


__all__ = ["ShadowAwareCanonicalSignalLedger"]
