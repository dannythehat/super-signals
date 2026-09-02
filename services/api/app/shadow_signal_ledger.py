"""Canonical signal ledger that admits isolated Provider Lab shadow sources.

The production semantic pipeline deliberately allows ``shadow`` sources so their
provider instructions can be studied. The ordinary canonical signal service still
protects broker execution by accepting only testing/live sources, so Provider Lab needs
an explicit ledger boundary that also admits shadow rows. The execution dispatcher keeps
shadow sources isolated and routes them only to the virtual benchmark.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.canonical_signal_ledger import CanonicalSignalLedger


class ShadowAwareCanonicalSignalLedger(CanonicalSignalLedger):
    """Create canonical evidence for testing/live plus non-broker shadow research."""

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
