"""Source-aware AI decision pipeline for Telegram provider sequences.

The base pipeline owns idempotency, persistence, canonical Signal creation and lifecycle
bridging. This subclass changes only the OpenAI interpretation input: each decision gets
the provider name, bounded recent history from the same source, and from Day 34 onward a
bounded broker-backed Active Trade Watch context for that same source.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.ai_message_pipeline import AiMessagePipeline
from app.ai_message_supervisor import AiMessageDecision, AiSupervisorError

_CONTEXT_LIMIT = 16
_CONTEXT_TEXT_LIMIT = 900
_ACTIVE_TRADE_LIMIT = 8
_ACTIVE_EVENT_LIMIT = 6


class SourceAwareAiMessagePipeline(AiMessagePipeline):
    """AI pipeline that understands each provider as a message and active-trade sequence."""

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
            active_trade_context = self._active_trade_context(source_id=source_id)
            try:
                decide_with_active_context = getattr(
                    self._supervisor,
                    "decide_with_active_context",
                    None,
                )
                if callable(decide_with_active_context):
                    return decide_with_active_context(
                        raw_text=raw_text,
                        source_status=source_status,
                        source_name=source_name,
                        active_trade_context=active_trade_context,
                        recent_source_messages=recent_source_messages,
                        reply_context=reply_context,
                        previous_text=previous_text,
                        is_edit=revision_index > 0,
                    )
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

    def _active_trade_context(self, *, source_id: UUID) -> list[dict[str, Any]]:
        """Return a privacy-safe Active Trade Watch for one logical Telegram source.

        A local ``positions.status='open'`` row with a mapped broker position ID exists
        only after confirmed broker placement. Day 27/32 reconciliation removes it from
        this active set when broker truth says the position no longer exists. Pending
        state is additionally read from the Day 33 broker-backed outcome model so this
        method is ready for a future supported pending-order path without treating
        ordinary price waiting as pending.

        The context deliberately contains no user/account IDs, balances, P&L, provider
        credentials or another source's trades. It is semantic context only; the Day 27
        lifecycle linker still owns fail-closed target resolution.
        """
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    WITH active_position_state AS (
                        SELECT
                            s.id AS signal_id,
                            s.symbol,
                            s.side,
                            s.order_type,
                            s.entry_low,
                            s.entry_high,
                            s.stop_loss AS provider_stop_loss,
                            s.take_profits,
                            s.provider_message_id,
                            s.source_posted_at,
                            ARRAY_AGG(DISTINCT p.tp_index ORDER BY p.tp_index)
                                FILTER (
                                    WHERE p.status = 'open'
                                      AND p.broker_position_id IS NOT NULL
                                ) AS open_tp_indices,
                            ARRAY_AGG(DISTINCT p.stop_loss ORDER BY p.stop_loss)
                                FILTER (
                                    WHERE p.status = 'open'
                                      AND p.broker_position_id IS NOT NULL
                                      AND p.stop_loss IS NOT NULL
                                ) AS current_stop_losses
                        FROM signals AS s
                        JOIN positions AS p ON p.signal_id = s.id
                        WHERE s.source_id = :source_id
                        GROUP BY
                            s.id, s.symbol, s.side, s.order_type, s.entry_low,
                            s.entry_high, s.stop_loss, s.take_profits,
                            s.provider_message_id, s.source_posted_at
                    ),
                    pending_state AS (
                        SELECT
                            o.signal_id,
                            ARRAY_AGG(DISTINCT p.tp_index ORDER BY p.tp_index)
                                FILTER (WHERE o.status = 'pending') AS pending_tp_indices
                        FROM performance_trade_outcomes AS o
                        JOIN positions AS p ON p.id = o.position_id
                        JOIN signals AS s ON s.id = o.signal_id
                        WHERE s.source_id = :source_id
                        GROUP BY o.signal_id
                    )
                    SELECT
                        a.*,
                        p.pending_tp_indices
                    FROM active_position_state AS a
                    LEFT JOIN pending_state AS p ON p.signal_id = a.signal_id
                    WHERE COALESCE(cardinality(a.open_tp_indices), 0) > 0
                       OR COALESCE(cardinality(p.pending_tp_indices), 0) > 0
                    ORDER BY a.source_posted_at DESC, a.provider_message_id DESC
                    LIMIT :limit
                    """
                ),
                {"source_id": source_id, "limit": _ACTIVE_TRADE_LIMIT},
            ).mappings().all()

            context: list[dict[str, Any]] = []
            for row in rows:
                events = session.execute(
                    text(
                        """
                        SELECT event_type, rendered_text, pips, occurred_at
                        FROM signal_lifecycle_events
                        WHERE signal_id = :signal_id
                        ORDER BY occurred_at DESC, created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"signal_id": row["signal_id"], "limit": _ACTIVE_EVENT_LIMIT},
                ).mappings().all()

                open_indices = [int(value) for value in (row["open_tp_indices"] or [])]
                pending_indices = [int(value) for value in (row["pending_tp_indices"] or [])]
                take_profits = [
                    self._plain_number(value)
                    for value in (row["take_profits"] or [])
                ]
                current_stops = [
                    self._plain_number(value)
                    for value in (row["current_stop_losses"] or [])
                ]
                context.append(
                    {
                        "signal_id": str(row["signal_id"]),
                        "broker_state": "open" if open_indices else "pending",
                        "symbol": str(row["symbol"] or ""),
                        "side": str(row["side"] or ""),
                        "order_type": str(row["order_type"] or ""),
                        "provider_message_id": int(row["provider_message_id"]),
                        "provider_posted_at": self._plain_datetime(row["source_posted_at"]),
                        "provider_entry_low": self._plain_number(row["entry_low"]),
                        "provider_entry_high": self._plain_number(row["entry_high"]),
                        "provider_stop_loss": self._plain_number(row["provider_stop_loss"]),
                        "provider_take_profits": take_profits,
                        "open_tp_indices": open_indices,
                        "pending_tp_indices": pending_indices,
                        "broker_reconciled_current_stop_losses": current_stops,
                        "recent_lifecycle": [
                            {
                                "event_type": str(event["event_type"] or ""),
                                "rendered_text": str(event["rendered_text"] or "")[:500],
                                "pips": self._plain_number(event["pips"]),
                                "occurred_at": self._plain_datetime(event["occurred_at"]),
                            }
                            for event in reversed(events)
                        ],
                    }
                )

        return context

    @staticmethod
    def _plain_number(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        return str(value)

    @staticmethod
    def _plain_datetime(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value or "")
