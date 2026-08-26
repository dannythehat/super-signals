"""Bridge provider updates into the canonical lifecycle ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.provider_pips_day34 import normalize_provider_pips
from app.standalone_lifecycle_linker import extract_update_symbol
from app.standalone_lifecycle_linker_v2 import StandaloneLifecycleLinkerV2

AI_LIFECYCLE_VERSION = "canonical-lifecycle-v1"


@dataclass(frozen=True, slots=True)
class AiLifecycleResult:
    created: bool
    linked: bool
    signal_id: UUID | None
    event_id: UUID | None
    reason: str


class AiLifecycleBridge:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def process(
        self,
        *,
        message_id: UUID,
        extracted: dict[str, Any],
        revision_index: int = 0,
    ) -> AiLifecycleResult:
        update_type = str(extracted.get("update_type") or "").strip()
        event_type, rendered_text = self._render(update_type, extracted)
        if event_type is None:
            return AiLifecycleResult(False, False, None, None, "provider_update_unsupported")

        raw_provider_pips = extracted.get("provider_claimed_pips")
        normalized_pips = normalize_provider_pips(raw_provider_pips)

        with self._session_factory() as session:
            row = self._message_revision_row(session, message_id, revision_index)
            if row is None:
                return AiLifecycleResult(False, False, None, None, "message_not_eligible")

            signal, link_reason = self._resolve_signal(
                session,
                row,
                revision_index=revision_index,
            )
            if signal is None:
                return AiLifecycleResult(False, False, None, None, link_reason)

            event_key = f"ai-provider:{message_id}:{revision_index}"
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
                        pips,
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
                        :pips,
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
                    "source_revision_index": revision_index,
                    "event_type": event_type,
                    "event_key": event_key,
                    "rendered_text": rendered_text,
                    "pips": normalized_pips,
                    "aggregate_result": json.dumps(
                        {
                            "ai_supervisor": True,
                            "source_revision_index": revision_index,
                            "lifecycle_link_reason": link_reason,
                            "update_target": extracted.get("update_target"),
                            "update_value": extracted.get("update_value"),
                            "provider_claimed_pips": raw_provider_pips,
                            "provider_claimed_pips_normalized": (
                                str(normalized_pips) if normalized_pips is not None else None
                            ),
                            "revised_instruction": extracted,
                        }
                    ),
                    "occurred_at": row["occurred_at"],
                },
            ).scalar_one_or_none()
            if event_id is None:
                existing = session.execute(
                    text("SELECT id FROM signal_lifecycle_events WHERE event_key=:event_key"),
                    {"event_key": event_key},
                ).scalar_one_or_none()
                return AiLifecycleResult(False, True, signal["id"], existing, "existing_event")

            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="signal.lifecycle_event_created.ai_supervisor",
                    entity_type="signal",
                    entity_id=signal["id"],
                    payload={
                        "lifecycle_version": AI_LIFECYCLE_VERSION,
                        "lifecycle_event_id": str(event_id),
                        "event_type": event_type,
                        "lifecycle_link_reason": link_reason,
                        "source_message_id": str(row["message_id"]),
                        "source_revision_index": revision_index,
                        "provider_claimed_pips_raw": raw_provider_pips,
                        "provider_claimed_pips_normalized": (
                            str(normalized_pips) if normalized_pips is not None else None
                        ),
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
            return AiLifecycleResult(True, True, signal["id"], event_id, "created")

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
                    m.telegram_message_id,
                    CASE WHEN :revision_index=0 THEN m.raw_text ELSE mr.raw_text END AS raw_text,
                    CASE WHEN :revision_index=0 THEN m.raw_payload ELSE mr.raw_payload END AS raw_payload,
                    CASE WHEN :revision_index=0 THEN m.posted_at ELSE mr.edited_at END AS occurred_at
                FROM messages AS m
                JOIN sources AS s ON s.id=m.source_id
                LEFT JOIN message_revisions AS mr
                  ON mr.message_id=m.id
                 AND mr.revision_index=:revision_index
                WHERE m.id=:message_id
                  AND m.deleted_at IS NULL
                  AND s.status IN ('testing','live')
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
                return original_signal, "original_signal_edit"

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
                        WHERE source_id=:source_id
                          AND provider_message_id=:provider_message_id
                        LIMIT 1
                        """
                    ),
                    {"source_id": row["source_id"], "provider_message_id": reply_id},
                ).mappings().first()
                if signal is not None:
                    return signal, "explicit_telegram_reply"

        symbol_hint = extract_update_symbol(str(row["raw_text"] or ""))
        active = session.execute(
            text(
                """
                SELECT DISTINCT s.id,s.symbol,s.provider_message_id,s.source_posted_at
                FROM signals AS s
                JOIN positions AS p ON p.signal_id=s.id
                LEFT JOIN performance_trade_outcomes AS o ON o.position_id=p.id
                WHERE s.source_id=:source_id
                  AND s.source_posted_at<=:occurred_at
                  AND (CAST(:symbol_hint AS text) IS NULL OR UPPER(s.symbol)=CAST(:symbol_hint AS text))
                  AND (
                      (p.status='open' AND p.broker_position_id IS NOT NULL
                       AND COALESCE(o.status,'open') NOT IN ('won','lost','breakeven','closed_unknown'))
                      OR o.status='pending'
                  )
                ORDER BY s.source_posted_at DESC,s.provider_message_id DESC
                """
            ),
            {
                "source_id": row["source_id"],
                "occurred_at": row["occurred_at"],
                "symbol_hint": symbol_hint,
            },
        ).mappings().all()
        if len(active) == 1:
            return active[0], "active_broker_unique"
        if len(active) > 1:
            return None, "active_trade_target_ambiguous"

        candidate, method, reason = StandaloneLifecycleLinkerV2._resolve_candidate(session, row)
        if candidate is not None:
            return candidate, method or "standalone_legacy_unique"
        if "ambiguous" in reason.lower():
            return None, "signal_link_ambiguous"
        return None, "signal_link_unresolved"

    @staticmethod
    def _render(update_type: str, extracted: dict[str, Any]) -> tuple[str | None, str]:
        target = extracted.get("update_target")
        value = extracted.get("update_value")
        if update_type == "tp_hit":
            return "take_profit_hit", f"TRADE UPDATE\n{target or 'Take-profit target'} reached."
        if update_type == "close":
            return "close_instruction", "TRADE UPDATE\nA separate instruction was received to close the remaining positions."
        if update_type == "close_half":
            return "partial_close", "TRADE UPDATE\nPartial close instructed."
        if update_type == "move_to_break_even":
            return "break_even", "TRADE UPDATE\nStop loss moved to entry on remaining positions."
        if update_type == "edit_stop_loss":
            suffix = f" to {value}" if value is not None else ""
            return "stop_change", f"TRADE UPDATE\nStop loss change instructed{suffix}."
        if update_type == "edit_take_profit":
            suffix = f" to {value}" if value is not None else ""
            return "take_profit_change", f"TRADE UPDATE\nTake-profit change instructed{suffix}."
        if update_type == "cancel_pending":
            return "cancel", "TRADE UPDATE\nPending order cancellation instructed."
        if update_type == "add_market":
            side = str(extracted.get("update_value") or "").strip().upper()
            suffix = f" {side}" if side in {"BUY", "SELL"} else ""
            return "add_market", f"TRADE UPDATE\nAdditional{suffix} market entry instructed."
        if update_type == "result_report":
            pips = extracted.get("provider_claimed_pips")
            suffix = f" ({pips} stated by provider)" if pips is not None else ""
            return "provider_result_report", f"TRADE RESULT UPDATE{suffix}."
        if update_type == "other":
            return "provider_update", "TRADE UPDATE\nProvider instruction revised or updated."
        return None, ""
