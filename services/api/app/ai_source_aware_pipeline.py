"""Source-aware AI decision pipeline for Telegram provider sequences.

The base pipeline owns idempotency, persistence, canonical Signal creation and lifecycle
bridging. This subclass changes only the OpenAI interpretation input: each decision gets
the provider name plus a bounded recent history from the same source so the model can
learn that provider's communication grammar without mixing providers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.ai_message_pipeline import AiMessagePipeline
from app.ai_message_supervisor import AiMessageDecision, AiSupervisorError

_CONTEXT_LIMIT = 16
_CONTEXT_TEXT_LIMIT = 900


class SourceAwareAiMessagePipeline(AiMessagePipeline):
    """AI pipeline that understands each provider as a message sequence."""

    def _decide(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
        raw_text: str,
        source_status: str,
        reply_context: str | None,
        previous_text: str | None,
    ) -> AiMessageDecision:
        if self._supervisor is not None:
            source_name, recent_source_messages = self._source_context(
                source_id=source_id,
                telegram_message_id=telegram_message_id,
            )
            try:
                return self._supervisor.decide(
                    raw_text=raw_text,
                    source_status=source_status,
                    source_name=source_name,
                    recent_source_messages=recent_source_messages,
                    reply_context=reply_context,
                    previous_text=previous_text,
                    is_edit=revision_index > 0,
                )
            except AiSupervisorError:
                pass

        return self._deterministic_fallback(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
        )

    def _source_context(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Return provider identity and recent same-source messages before this post.

        Context is deliberately bounded and observational. It is supplied to the model
        to understand provider grammar. The execution guard still accepts numeric trade
        evidence only from the current message and its direct Telegram reply context.
        """
        with self._session_factory() as session:
            source_name = session.execute(
                text(
                    """
                    SELECT COALESCE(NULLIF(chat_title, ''), NULLIF(source_alias, ''))
                    FROM sources
                    WHERE id = :source_id
                    """
                ),
                {"source_id": source_id},
            ).scalar_one_or_none()

            rows = session.execute(
                text(
                    """
                    SELECT telegram_message_id, posted_at, raw_text, raw_payload
                    FROM messages
                    WHERE source_id = :source_id
                      AND telegram_message_id < :telegram_message_id
                      AND deleted_at IS NULL
                    ORDER BY telegram_message_id DESC
                    LIMIT :limit
                    """
                ),
                {
                    "source_id": source_id,
                    "telegram_message_id": telegram_message_id,
                    "limit": _CONTEXT_LIMIT,
                },
            ).mappings().all()

        context: list[dict[str, Any]] = []
        for row in reversed(rows):
            raw_payload = row["raw_payload"] if isinstance(row["raw_payload"], dict) else {}
            reply_to = raw_payload.get("reply_to_message_id") if raw_payload else None
            posted_at = row["posted_at"]
            context.append(
                {
                    "telegram_message_id": int(row["telegram_message_id"]),
                    "posted_at": (
                        posted_at.isoformat()
                        if isinstance(posted_at, datetime)
                        else str(posted_at or "")
                    ),
                    "reply_to_message_id": (
                        int(reply_to) if isinstance(reply_to, int) else reply_to
                    ),
                    "text": str(row["raw_text"] or "")[:_CONTEXT_TEXT_LIMIT],
                }
            )

        return (str(source_name) if source_name else None, context)
