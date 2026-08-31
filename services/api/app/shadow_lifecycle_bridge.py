"""Lifecycle linking that treats shadow providers as first-class research sources."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai_lifecycle_bridge import AiLifecycleBridge


class ShadowAwareAiLifecycleBridge(AiLifecycleBridge):
    """Link shadow management to isolated active shadow signals without broker positions."""

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
                    m.id AS message_id,m.source_id,m.telegram_message_id,s.status AS source_status,
                    CASE WHEN :revision_index=0 THEN m.raw_text ELSE mr.raw_text END AS raw_text,
                    CASE WHEN :revision_index=0 THEN m.raw_payload ELSE mr.raw_payload END AS raw_payload,
                    CASE WHEN :revision_index=0 THEN m.posted_at ELSE mr.edited_at END AS occurred_at
                FROM messages AS m
                JOIN sources AS s ON s.id=m.source_id
                LEFT JOIN message_revisions AS mr
                  ON mr.message_id=m.id AND mr.revision_index=:revision_index
                WHERE m.id=:message_id
                  AND m.deleted_at IS NULL
                  AND s.status IN ('testing','shadow','live')
                  AND (:revision_index=0 OR mr.revision_index IS NOT NULL)
                """
            ),
            {"message_id": message_id, "revision_index": revision_index},
        ).mappings().first()

    @staticmethod
    def _resolve_signal(
        session: Session,
        row: Any,
        *,
        revision_index: int,
    ) -> tuple[Any | None, str]:
        if str(row.get("source_status") or "") != "shadow":
            return AiLifecycleBridge._resolve_signal(
                session,
                row,
                revision_index=revision_index,
            )

        if revision_index > 0:
            original_signal = session.execute(
                text(
                    """
                    SELECT id,symbol,provider_message_id,source_posted_at
                    FROM signals WHERE source_message_id=:message_id LIMIT 1
                    """
                ),
                {"message_id": row["message_id"]},
            ).mappings().first()
            if original_signal is not None:
                return original_signal, "shadow_original_signal_edit"

        payload = row["raw_payload"] if isinstance(row["raw_payload"], dict) else {}
        reply_value = payload.get("reply_to_message_id")
        if reply_value is not None:
            try:
                reply_id = int(reply_value)
            except (TypeError, ValueError):
                reply_id = None
            if reply_id is not None:
                signal = session.execute(
                    text(
                        """
                        SELECT id,symbol,provider_message_id,source_posted_at
                        FROM signals
                        WHERE source_id=:source_id AND provider_message_id=:provider_message_id
                        LIMIT 1
                        """
                    ),
                    {"source_id": row["source_id"], "provider_message_id": reply_id},
                ).mappings().first()
                if signal is not None:
                    return signal, "shadow_explicit_telegram_reply"

        active = session.execute(
            text(
                """
                SELECT DISTINCT s.id,s.symbol,s.provider_message_id,s.source_posted_at
                FROM shadow_trades st
                JOIN signals s ON s.id=st.signal_id
                WHERE st.source_id=:source_id
                  AND st.status IN ('pending','open')
                  AND s.source_posted_at<=:occurred_at
                ORDER BY s.source_posted_at DESC,s.provider_message_id DESC
                """
            ),
            {"source_id": row["source_id"], "occurred_at": row["occurred_at"]},
        ).mappings().all()
        if len(active) == 1:
            return active[0], "shadow_active_unique"
        if len(active) > 1:
            return None, "shadow_active_trade_target_ambiguous"

        # No open shadow position exists. Fall through to the ordinary chronology/reply
        # resolver for provider result posts that arrive just after a trade closed.
        return AiLifecycleBridge._resolve_signal(
            session,
            row,
            revision_index=revision_index,
        )


__all__ = ["ShadowAwareAiLifecycleBridge"]
