"""Day 20 publisher: mirror stored lifecycle events as replies to the Signal post."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from sqlalchemy import text

from app.telegram_publisher import (
    PublicationAttempt,
    TelegramPublishError,
    _bot_api_call,
    render_signal_post,
)
from app.telegram_publisher_policy import Day19TelegramPublisherManager

TESTING_BANNER = "✨🧪 TESTING 🧪✨\nDEMO / NOT LIVE\n\n"


def apply_source_status_banner(rendered_text: str, source_status: str) -> str:
    """Make testing posts unmistakable without exposing provider identity."""

    return (
        f"{TESTING_BANNER}{rendered_text}"
        if source_status.strip().lower() == "testing"
        else rendered_text
    )


@dataclass(frozen=True, slots=True)
class LifecyclePublicationAttempt(PublicationAttempt):
    reply_to_message_id: int


class Day20TelegramPublisherManager(Day19TelegramPublisherManager):
    """Day 19 publisher plus one idempotent reply per stored lifecycle event."""

    def _seed_missing_publications(self) -> None:
        # Day 20 replaces Day 19's signal/kind unique constraint with separate
        # root-Signal and lifecycle-event idempotency indexes. Seed only active
        # testing/live sources; PAUSED sources must not produce group posts.
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO telegram_publications (
                        signal_id,
                        publication_kind,
                        status
                    )
                    SELECT sig.id, 'signal_created', 'pending'
                    FROM signals AS sig
                    JOIN sources AS src ON src.id = sig.source_id
                    LEFT JOIN telegram_publications AS pub
                      ON pub.signal_id = sig.id
                     AND pub.publication_kind = 'signal_created'
                     AND pub.lifecycle_event_id IS NULL
                    WHERE pub.id IS NULL
                      AND src.status IN ('testing', 'live')
                    ON CONFLICT DO NOTHING
                    """
                )
            )
            session.execute(
                text(
                    """
                    INSERT INTO telegram_publications (
                        signal_id,
                        lifecycle_event_id,
                        publication_kind,
                        status
                    )
                    SELECT
                        ev.signal_id,
                        ev.id,
                        'lifecycle_event',
                        'pending'
                    FROM signal_lifecycle_events AS ev
                    JOIN signals AS sig ON sig.id = ev.signal_id
                    JOIN sources AS src ON src.id = sig.source_id
                    LEFT JOIN telegram_publications AS pub
                      ON pub.lifecycle_event_id = ev.id
                    WHERE pub.id IS NULL
                      AND src.status IN ('testing', 'live')
                    ON CONFLICT DO NOTHING
                    """
                )
            )
            session.commit()

    def _claim_root(self) -> PublicationAttempt | None:
        assert self._destination_chat_id is not None
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        pub.id AS publication_id,
                        pub.signal_id,
                        sig.symbol,
                        sig.side,
                        sig.entry_low AS entry_price,
                        sig.stop_loss,
                        sig.take_profits,
                        sig.risk_multiplier,
                        src.status AS source_status
                    FROM telegram_publications AS pub
                    JOIN signals AS sig ON sig.id = pub.signal_id
                    JOIN sources AS src ON src.id = sig.source_id
                    WHERE pub.status = 'pending'
                      AND pub.publication_kind = 'signal_created'
                      AND src.status IN ('testing', 'live')
                    ORDER BY pub.created_at ASC
                    FOR UPDATE OF pub SKIP LOCKED
                    LIMIT 1
                    """
                )
            ).mappings().first()
            if row is None:
                session.rollback()
                return None

            rendered = apply_source_status_banner(
                render_signal_post(row), str(row["source_status"])
            )
            session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET status = 'sending',
                        rendered_text = :rendered_text,
                        destination_chat_id = :destination_chat_id,
                        attempt_count = attempt_count + 1,
                        attempted_at = now(),
                        failure_code = NULL,
                        failure_reason = NULL,
                        updated_at = now()
                    WHERE id = :publication_id
                      AND status = 'pending'
                    """
                ),
                {
                    "publication_id": row["publication_id"],
                    "rendered_text": rendered,
                    "destination_chat_id": self._destination_chat_id,
                },
            )
            session.commit()
            return PublicationAttempt(
                publication_id=row["publication_id"],
                signal_id=row["signal_id"],
                text=rendered,
            )

    def _claim_next(self) -> PublicationAttempt | None:
        # Always ensure a root Signal post is claimed before any reply beneath it.
        root_attempt = self._claim_root()
        if root_attempt is not None:
            return root_attempt

        assert self._destination_chat_id is not None
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        pub.id AS publication_id,
                        pub.signal_id,
                        ev.rendered_text,
                        root.telegram_message_id AS reply_to_message_id,
                        src.status AS source_status
                    FROM telegram_publications AS pub
                    JOIN signal_lifecycle_events AS ev
                      ON ev.id = pub.lifecycle_event_id
                    JOIN signals AS sig ON sig.id = pub.signal_id
                    JOIN sources AS src ON src.id = sig.source_id
                    JOIN telegram_publications AS root
                      ON root.signal_id = pub.signal_id
                     AND root.publication_kind = 'signal_created'
                     AND root.lifecycle_event_id IS NULL
                    WHERE pub.status = 'pending'
                      AND pub.publication_kind = 'lifecycle_event'
                      AND root.status = 'sent'
                      AND root.telegram_message_id IS NOT NULL
                      AND src.status IN ('testing', 'live')
                    ORDER BY ev.occurred_at ASC, ev.created_at ASC
                    FOR UPDATE OF pub SKIP LOCKED
                    LIMIT 1
                    """
                )
            ).mappings().first()
            if row is None:
                session.rollback()
                return None

            rendered = apply_source_status_banner(
                str(row["rendered_text"]), str(row["source_status"])
            )
            session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET status = 'sending',
                        rendered_text = :rendered_text,
                        destination_chat_id = :destination_chat_id,
                        reply_to_telegram_message_id = :reply_to_message_id,
                        attempt_count = attempt_count + 1,
                        attempted_at = now(),
                        failure_code = NULL,
                        failure_reason = NULL,
                        updated_at = now()
                    WHERE id = :publication_id
                      AND status = 'pending'
                    """
                ),
                {
                    "publication_id": row["publication_id"],
                    "rendered_text": rendered,
                    "destination_chat_id": self._destination_chat_id,
                    "reply_to_message_id": int(row["reply_to_message_id"]),
                },
            )
            session.commit()
            return LifecyclePublicationAttempt(
                publication_id=row["publication_id"],
                signal_id=row["signal_id"],
                text=rendered,
                reply_to_message_id=int(row["reply_to_message_id"]),
            )

    async def _deliver(self, attempt: PublicationAttempt) -> None:
        if not isinstance(attempt, LifecyclePublicationAttempt):
            await super()._deliver(attempt)
            return

        assert self._bot_token is not None
        assert self._destination_chat_id is not None
        try:
            result = await asyncio.to_thread(
                _bot_api_call,
                self._bot_token,
                "sendMessage",
                {
                    "chat_id": self._destination_chat_id,
                    "text": attempt.text,
                    "disable_web_page_preview": "true",
                    "reply_parameters": json.dumps(
                        {
                            "message_id": attempt.reply_to_message_id,
                            "allow_sending_without_reply": False,
                        }
                    ),
                },
            )
            telegram_message_id = int(result["message_id"])
        except (TelegramPublishError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, TelegramPublishError):
                code, reason = exc.code, exc.reason
            else:
                code = "telegram_invalid_success_response"
                reason = "Telegram did not return a usable destination message ID."
            await asyncio.to_thread(self._record_failure, attempt, code, reason)
            return

        await asyncio.to_thread(self._record_success, attempt, telegram_message_id)
