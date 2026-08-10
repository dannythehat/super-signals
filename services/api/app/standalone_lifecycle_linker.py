"""Safe Day 20 fallback for provider trade updates posted without Telegram replies.

Some signal providers post management updates as new messages instead of using
Telegram's reply feature. This module links those updates only when the source
context is unambiguous. Explicit replies remain owned by SignalLifecycleService.

No Position, broker action, pips calculation, or arbitrary provider text is
created here.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.signal_lifecycle import render_provider_update

STANDALONE_LIFECYCLE_VERSION = "day20-standalone-v1"
RECENT_CONTEXT = timedelta(minutes=90)
_SYMBOL_HINT = re.compile(r"\b(?:XAU\s*/?\s*USD|XAUUSD|GOLD)\b", re.IGNORECASE)


def extract_update_symbol(raw_text: str) -> str | None:
    """Map supported provider symbol wording to the canonical Day 20 symbol."""

    return "XAUUSD" if _SYMBOL_HINT.search(raw_text or "") else None


class StandaloneLifecycleLinker:
    """Handle classified trade updates that have no explicit Telegram reply."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def process_original(self, source_id: UUID, telegram_message_id: int) -> bool:
        """Return True when this was a standalone trade update and was handled."""

        with self._session_factory() as session:
            row = self._update_row(
                session,
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=0,
            )
            if row is None or self._reply_id(row["raw_payload"]) is not None:
                return False
            self._process_row(session, row)
            session.commit()
            return True

    def process_latest_revision(self, source_id: UUID, telegram_message_id: int) -> bool:
        """Handle the latest standalone edit; explicit-reply edits stay on exact path."""

        with self._session_factory() as session:
            revision_index = session.execute(
                text(
                    """
                    SELECT COALESCE(MAX(mr.revision_index), 0)
                    FROM messages AS m
                    LEFT JOIN message_revisions AS mr ON mr.message_id = m.id
                    WHERE m.source_id = :source_id
                      AND m.telegram_message_id = :telegram_message_id
                    """
                ),
                {
                    "source_id": source_id,
                    "telegram_message_id": telegram_message_id,
                },
            ).scalar_one_or_none()
            if revision_index is None:
                return False
            row = self._update_row(
                session,
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=int(revision_index),
            )
            if row is None or self._reply_id(row["raw_payload"]) is not None:
                return False
            self._process_row(session, row)
            session.commit()
            return True

    @staticmethod
    def _update_row(
        session: Session,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
    ) -> Any | None:
        return session.execute(
            text(
                """
                SELECT
                    m.id AS message_id,
                    m.source_id,
                    m.telegram_message_id,
                    mc.revision_index,
                    CASE
                        WHEN mc.revision_index = 0 THEN m.raw_text
                        ELSE mr.raw_text
                    END AS raw_text,
                    CASE
                        WHEN mc.revision_index = 0 THEN m.raw_payload
                        ELSE mr.raw_payload
                    END AS raw_payload,
                    CASE
                        WHEN mc.revision_index = 0 THEN m.posted_at
                        ELSE mr.edited_at
                    END AS occurred_at,
                    mc.matched_rules
                FROM messages AS m
                JOIN message_classifications AS mc
                  ON mc.message_id = m.id
                 AND mc.revision_index = :revision_index
                LEFT JOIN message_revisions AS mr
                  ON mr.message_id = m.id
                 AND mr.revision_index = mc.revision_index
                WHERE m.source_id = :source_id
                  AND m.telegram_message_id = :telegram_message_id
                  AND m.deleted_at IS NULL
                  AND mc.classification = 'trade_update'
                  AND mc.decision_status = 'classified'
                  AND (
                      mc.revision_index = 0
                      OR mr.revision_index IS NOT NULL
                  )
                """
            ),
            {
                "source_id": source_id,
                "telegram_message_id": telegram_message_id,
                "revision_index": revision_index,
            },
        ).mappings().first()

    def _process_row(self, session: Session, row: Any) -> None:
        rules = self._rules(row["matched_rules"])
        rendered = render_provider_update(str(row["raw_text"] or ""), rules)
        if rendered is None:
            self._audit_stopped(
                session,
                row=row,
                reason="Standalone trade update contains no supported Day 20 lifecycle action.",
            )
            return

        signal, link_method, reason = self._resolve_candidate(session, row)
        if signal is None:
            self._audit_stopped(session, row=row, reason=reason)
            return

        event_key = f"provider:{row['message_id']}:{int(row['revision_index'])}"
        event_id = session.execute(
            text(
                """
                INSERT INTO signal_lifecycle_events (
                    signal_id,
                    source_message_id,
                    source_revision_index,
                    event_type,
                    event_key,
                    origin,
                    rendered_text,
                    aggregate_result,
                    occurred_at
                )
                VALUES (
                    :signal_id,
                    :source_message_id,
                    :source_revision_index,
                    :event_type,
                    :event_key,
                    'provider_update',
                    :rendered_text,
                    CAST(:aggregate_result AS jsonb),
                    :occurred_at
                )
                ON CONFLICT (event_key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "signal_id": signal["id"],
                "source_message_id": row["message_id"],
                "source_revision_index": int(row["revision_index"]),
                "event_type": rendered.event_type,
                "event_key": event_key,
                "rendered_text": rendered.text,
                "aggregate_result": json.dumps({}),
                "occurred_at": row["occurred_at"],
            },
        ).scalar_one_or_none()
        if event_id is None:
            return

        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="signal.lifecycle_event_created",
                entity_type="signal",
                entity_id=signal["id"],
                payload={
                    "lifecycle_version": STANDALONE_LIFECYCLE_VERSION,
                    "lifecycle_event_id": str(event_id),
                    "event_type": rendered.event_type,
                    "source_message_id": str(row["message_id"]),
                    "source_revision_index": int(row["revision_index"]),
                    "link_method": link_method,
                    "provider_identity_exposed": False,
                    "position_created": False,
                    "trade_action_created": False,
                    "pips_calculated": False,
                },
            )
        )

    @staticmethod
    def _resolve_candidate(session: Session, row: Any) -> tuple[Any | None, str | None, str]:
        symbol_hint = extract_update_symbol(str(row["raw_text"] or ""))
        candidates = session.execute(
            text(
                """
                SELECT s.id, s.symbol, s.provider_message_id, s.source_posted_at
                FROM signals AS s
                WHERE s.source_id = :source_id
                  AND s.source_posted_at <= :occurred_at
                  AND (:symbol_hint IS NULL OR UPPER(s.symbol) = :symbol_hint)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM signal_lifecycle_events AS terminal_event
                      WHERE terminal_event.signal_id = s.id
                        AND terminal_event.event_type IN (
                            'stop_loss_hit', 'cancel', 'close_instruction'
                        )
                  )
                ORDER BY s.source_posted_at DESC, s.provider_message_id DESC
                """
            ),
            {
                "source_id": row["source_id"],
                "occurred_at": row["occurred_at"],
                "symbol_hint": symbol_hint,
            },
        ).mappings().all()

        if len(candidates) == 1:
            method = "standalone_unique_symbol" if symbol_hint else "standalone_unique_active"
            return candidates[0], method, "Standalone trade update linked unambiguously."

        if len(candidates) > 1:
            cutoff = row["occurred_at"] - RECENT_CONTEXT
            recent = [candidate for candidate in candidates if candidate["source_posted_at"] >= cutoff]
            if len(recent) == 1:
                method = "standalone_recent_symbol_context" if symbol_hint else "standalone_recent_context"
                return recent[0], method, "Standalone trade update linked by unique recent context."

        symbol_detail = f" for {symbol_hint}" if symbol_hint else ""
        if not candidates:
            return (
                None,
                None,
                f"Standalone trade update has no active canonical Signal candidate{symbol_detail}.",
            )
        return (
            None,
            None,
            "Standalone trade update is ambiguous: more than one plausible active canonical "
            f"Signal exists in this source{symbol_detail}.",
        )

    @staticmethod
    def _reply_id(payload: Any) -> int | None:
        if not isinstance(payload, dict):
            return None
        value = payload.get("reply_to_message_id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _rules(value: Any) -> tuple[str, ...]:
        if isinstance(value, list):
            return tuple(str(item) for item in value)
        if isinstance(value, tuple):
            return tuple(str(item) for item in value)
        return ()

    @staticmethod
    def _audit_stopped(session: Session, *, row: Any, reason: str) -> None:
        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="signal.lifecycle_update_stopped",
                entity_type="message",
                entity_id=row["message_id"],
                payload={
                    "lifecycle_version": STANDALONE_LIFECYCLE_VERSION,
                    "source_revision_index": int(row["revision_index"]),
                    "reason": reason,
                    "signal_changed": False,
                    "position_created": False,
                    "trade_action_created": False,
                    "published": False,
                },
            )
        )
