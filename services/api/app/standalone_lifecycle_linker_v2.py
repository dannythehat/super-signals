"""Day 20 hardened standalone lifecycle matching.

This revision fixes PostgreSQL type inference for symbol-less updates and adds a
small restart recovery window for classified standalone updates that were stored
before lifecycle linking completed. Exact Telegram replies still use the primary
SignalLifecycleService path.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.standalone_lifecycle_linker import (
    RECENT_CONTEXT,
    StandaloneLifecycleLinker,
    extract_update_symbol,
)


class StandaloneLifecycleLinkerV2(StandaloneLifecycleLinker):
    """Standalone linker with typed SQL and idempotent recent recovery."""

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
                  AND (
                      CAST(:symbol_hint AS text) IS NULL
                      OR UPPER(s.symbol) = CAST(:symbol_hint AS text)
                  )
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

    def recover_recent(self) -> int:
        """Recover recently stored standalone updates after a listener crash/restart.

        The 30-minute window is deliberately narrow so enabling the Day 20 fallback
        cannot retroactively publish old historical provider updates. Event keys are
        already idempotent, and previously stopped ambiguous rows are excluded.
        """

        handled = 0
        with self._session_factory() as session:
            rows = session.execute(
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
                    FROM message_classifications AS mc
                    JOIN messages AS m ON m.id = mc.message_id
                    LEFT JOIN message_revisions AS mr
                      ON mr.message_id = mc.message_id
                     AND mr.revision_index = mc.revision_index
                    WHERE m.deleted_at IS NULL
                      AND mc.classification = 'trade_update'
                      AND mc.decision_status = 'classified'
                      AND COALESCE(
                          CASE WHEN mc.revision_index = 0 THEN m.posted_at ELSE mr.edited_at END,
                          m.created_at
                      ) >= NOW() - INTERVAL '30 minutes'
                      AND COALESCE(
                          CASE WHEN mc.revision_index = 0 THEN m.raw_payload ELSE mr.raw_payload END,
                          '{}'::jsonb
                      ) ->> 'reply_to_message_id' IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM signal_lifecycle_events AS existing_event
                          WHERE existing_event.source_message_id = m.id
                            AND existing_event.source_revision_index = mc.revision_index
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM audit_events AS stopped
                          WHERE stopped.entity_type = 'message'
                            AND stopped.entity_id = m.id
                            AND stopped.event_type = 'signal.lifecycle_update_stopped'
                            AND COALESCE(stopped.payload ->> 'lifecycle_version', '') LIKE 'day20-standalone%'
                            AND COALESCE((stopped.payload ->> 'source_revision_index')::integer, 0)
                                = mc.revision_index
                      )
                    ORDER BY occurred_at ASC, m.telegram_message_id ASC
                    """
                )
            ).mappings().all()

            for row in rows:
                self._process_row(session, row)
                handled += 1

            session.commit()
        return handled
