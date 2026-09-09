"""Fail-closed provider-wide management fanout for canonical broker routing.

The ordinary lifecycle bridge intentionally refuses a bare management post when more than
one provider signal is active because choosing one trade would be ambiguous.  Some provider
messages remove that ambiguity explicitly, e.g. ``MOVE ALL YOUR GOLD STOPLOSSES TO 4368``.
Those messages must apply to every active signal from that provider, not be discarded.

This dispatcher preserves the normal one-signal path and only fans out when the current
message is explicit about ALL positions for a named instrument and the durable AI decision
contains mechanically supported all-target management actions.  One immutable lifecycle
event is created per active signal and the existing canonical management service executes
each event with its normal idempotency, broker reconciliation and member-routing safeguards.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.execution_dispatch_canonical import (
    CanonicalExecutionDispatcher,
    CanonicalRouteResult,
    StoredDecision,
)
from app.models import AuditEvent
from app.standalone_lifecycle_linker import extract_update_symbol

logger = logging.getLogger(__name__)

_FORCED_LIFECYCLE_EVENT: ContextVar[tuple[UUID, UUID] | None] = ContextVar(
    "super_signals_forced_collective_lifecycle_event",
    default=None,
)

_ALLOWED_COLLECTIVE_ACTIONS = {
    "close",
    "move_to_break_even",
    "edit_stop_loss",
    "edit_take_profit",
    "cancel_pending",
}


class CollectiveAwareCanonicalExecutionDispatcher(CanonicalExecutionDispatcher):
    """Extend canonical routing with explicit provider-wide management fanout only."""

    def _resolve_lifecycle_event(
        self,
        message_id: UUID,
        revision_index: int,
    ) -> tuple[UUID | None, UUID | None]:
        forced = _FORCED_LIFECYCLE_EVENT.get()
        if forced is not None:
            return forced
        return super()._resolve_lifecycle_event(message_id, revision_index)

    async def _dispatch_management(
        self,
        stored: StoredDecision,
        revision_index: int,
    ) -> CanonicalRouteResult:
        events = self._resolve_lifecycle_events(stored.message_id, revision_index)
        if not events:
            created = self._materialize_collective_events(stored, revision_index)
            if created:
                events = self._resolve_lifecycle_events(stored.message_id, revision_index)

        # Preserve the original fail-closed path unless this message has multiple
        # lifecycle rows which were explicitly created as one provider-wide command.
        if len(events) <= 1 or not all(item[2] for item in events):
            return await super()._dispatch_management(stored, revision_index)

        results: list[CanonicalRouteResult] = []
        for event_id, signal_id, _is_collective in events:
            token = _FORCED_LIFECYCLE_EVENT.set((event_id, signal_id))
            try:
                results.append(await super()._dispatch_management(stored, revision_index))
            finally:
                _FORCED_LIFECYCLE_EVENT.reset(token)

        broker_actions = sum(item.broker_actions_sent for item in results)
        blocked = [item for item in results if item.outcome == "blocked"]
        if blocked:
            error_code = (
                "collective_management_partial_failure"
                if len(blocked) < len(results)
                else (blocked[0].error_code or "collective_management_failed")
            )
            logger.error(
                "Provider-wide management incomplete message=%s revision=%s targets=%s blocked=%s",
                stored.message_id,
                revision_index,
                len(results),
                len(blocked),
            )
            return CanonicalRouteResult(
                outcome="blocked",
                decision=stored.decision,
                action=stored.action,
                broker_actions_sent=broker_actions,
                error_code=error_code,
                reason=error_code,
            )

        if all(item.outcome == "already_applied" for item in results):
            return CanonicalRouteResult(
                outcome="already_applied",
                decision=stored.decision,
                action=stored.action,
                broker_actions_sent=broker_actions,
                already_applied=True,
                reason="collective_management_already_applied",
            )

        if all(item.outcome == "ignored" for item in results):
            return CanonicalRouteResult(
                outcome="ignored",
                decision=stored.decision,
                action=stored.action,
                broker_actions_sent=broker_actions,
                reason="collective_management_no_mapped_exposure",
            )

        logger.info(
            "Provider-wide management applied message=%s revision=%s targets=%s broker_actions=%s",
            stored.message_id,
            revision_index,
            len(results),
            broker_actions,
        )
        return CanonicalRouteResult(
            outcome="managed",
            decision=stored.decision,
            action=stored.action,
            broker_actions_sent=broker_actions,
            reason="collective_management_applied",
        )

    def _resolve_lifecycle_events(
        self,
        message_id: UUID,
        revision_index: int,
    ) -> tuple[tuple[UUID, UUID, bool], ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, signal_id, aggregate_result
                    FROM signal_lifecycle_events
                    WHERE source_message_id=:message_id
                      AND source_revision_index=:revision_index
                      AND origin='provider_update'
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"message_id": message_id, "revision_index": revision_index},
            ).mappings().all()
        resolved: list[tuple[UUID, UUID, bool]] = []
        for row in rows:
            aggregate = row["aggregate_result"] if isinstance(row["aggregate_result"], dict) else {}
            resolved.append(
                (
                    UUID(str(row["id"])),
                    UUID(str(row["signal_id"])),
                    bool(aggregate.get("collective_provider_management")),
                )
            )
        return tuple(resolved)

    def _materialize_collective_events(
        self,
        stored: StoredDecision,
        revision_index: int,
    ) -> int:
        if stored.decision != "trade_update" or stored.action != "apply_update":
            return 0

        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        m.source_id,
                        CASE WHEN :revision_index=0 THEN m.raw_text ELSE mr.raw_text END AS raw_text,
                        CASE WHEN :revision_index=0 THEN m.posted_at ELSE mr.edited_at END AS occurred_at,
                        d.extracted
                    FROM messages AS m
                    JOIN sources AS s ON s.id=m.source_id
                    JOIN ai_message_decisions AS d
                      ON d.message_id=m.id
                     AND d.revision_index=:revision_index
                    LEFT JOIN message_revisions AS mr
                      ON mr.message_id=m.id
                     AND mr.revision_index=:revision_index
                    WHERE m.id=:message_id
                      AND m.deleted_at IS NULL
                      AND s.status IN ('testing','live')
                      AND (:revision_index=0 OR mr.revision_index IS NOT NULL)
                    LIMIT 1
                    """
                ),
                {"message_id": stored.message_id, "revision_index": revision_index},
            ).mappings().first()
            if row is None:
                return 0

            extracted = row["extracted"] if isinstance(row["extracted"], dict) else {}
            raw_text = str(row["raw_text"] or "")
            if not self._is_explicit_collective_management(raw_text, extracted):
                return 0

            symbol = extract_update_symbol(raw_text)
            if not symbol:
                candidate = str(extracted.get("symbol") or "").strip().upper()
                symbol = candidate or ("XAUUSD" if re.search(r"\bGOLD\b", raw_text, re.I) else None)
            if not symbol:
                return 0
            symbol = str(symbol).upper()

            signals = session.execute(
                text(
                    """
                    SELECT DISTINCT s.id, s.source_posted_at
                    FROM signals AS s
                    WHERE s.source_id=:source_id
                      AND s.source_posted_at<=:occurred_at
                      AND UPPER(s.symbol)=:symbol
                      AND EXISTS (
                          SELECT 1
                          FROM positions AS p
                          LEFT JOIN performance_trade_outcomes AS o ON o.position_id=p.id
                          WHERE p.signal_id=s.id
                            AND (
                                (p.status='open' AND p.broker_position_id IS NOT NULL
                                 AND COALESCE(o.status,'open') NOT IN
                                     ('won','lost','breakeven','closed_unknown'))
                                OR (p.status='pending' AND p.broker_order_id IS NOT NULL)
                            )
                      )
                    ORDER BY s.source_posted_at ASC, s.id ASC
                    """
                ),
                {
                    "source_id": row["source_id"],
                    "occurred_at": row["occurred_at"],
                    "symbol": symbol,
                },
            ).mappings().all()
            if not signals:
                return 0

            event_type = self._collective_event_type(extracted)
            aggregate = {
                "ai_supervisor": True,
                "source_revision_index": revision_index,
                "lifecycle_link_reason": "explicit_provider_wide_management",
                "collective_provider_management": True,
                "collective_target_count": len(signals),
                "collective_symbol": symbol,
                "update_target": extracted.get("update_target"),
                "update_value": extracted.get("update_value"),
                "provider_claimed_pips": extracted.get("provider_claimed_pips"),
                "revised_instruction": extracted,
            }
            rendered_text = (
                "TRADE UPDATE\n"
                f"Provider-wide {symbol} management instruction for all active trades.\n"
                "Broker execution confirmation follows separately."
            )

            created = 0
            for signal in signals:
                signal_id = UUID(str(signal["id"]))
                event_key = (
                    f"ai-provider-collective:{stored.message_id}:{revision_index}:{signal_id}"
                )
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
                        ) VALUES (
                            :signal_id,
                            :source_message_id,
                            :source_revision_index,
                            :event_type,
                            :event_key,
                            'provider_update',
                            :rendered_text,
                            NULL,
                            CAST(:aggregate_result AS jsonb),
                            :occurred_at
                        )
                        ON CONFLICT (event_key) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "signal_id": signal_id,
                        "source_message_id": stored.message_id,
                        "source_revision_index": revision_index,
                        "event_type": event_type,
                        "event_key": event_key,
                        "rendered_text": rendered_text,
                        "aggregate_result": json.dumps(aggregate),
                        "occurred_at": row["occurred_at"],
                    },
                ).scalar_one_or_none()
                if event_id is None:
                    continue
                created += 1
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_type="signal.lifecycle_event_created.collective_management",
                        entity_type="signal",
                        entity_id=signal_id,
                        payload={
                            "lifecycle_event_id": str(event_id),
                            "source_message_id": str(stored.message_id),
                            "source_revision_index": revision_index,
                            "collective_symbol": symbol,
                            "collective_target_count": len(signals),
                            "trade_action_created": False,
                        },
                    )
                )
            session.commit()

        if created:
            logger.info(
                "Materialized provider-wide management message=%s revision=%s symbol=%s targets=%s",
                stored.message_id,
                revision_index,
                symbol,
                created,
            )
        return created

    @staticmethod
    def _management_actions(extracted: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        raw_actions = extracted.get("management_actions")
        if isinstance(raw_actions, list):
            actions = tuple(item for item in raw_actions if isinstance(item, dict))
            if actions:
                return actions
        update_type = str(extracted.get("update_type") or "").strip().lower()
        if update_type:
            return (
                {
                    "type": update_type,
                    "target": extracted.get("update_target") or "all",
                    "value": extracted.get("update_value"),
                },
            )
        return ()

    @classmethod
    def _is_explicit_collective_management(
        cls,
        raw_text: str,
        extracted: dict[str, Any],
    ) -> bool:
        upper = raw_text.upper()
        if re.search(r"\bALL\b", upper) is None:
            return False
        if re.search(r"\b(?:GOLD|XAUUSD)\b", upper) is None:
            return False

        actions = cls._management_actions(extracted)
        if not actions:
            return False
        for action in actions:
            action_type = str(action.get("type") or "").strip().lower()
            target = str(action.get("target") or "").strip().lower()
            if action_type not in _ALLOWED_COLLECTIVE_ACTIONS or target != "all":
                return False
            if action_type in {"edit_stop_loss", "edit_take_profit"}:
                if not str(action.get("value") or "").strip():
                    return False
        return True

    @classmethod
    def _collective_event_type(cls, extracted: dict[str, Any]) -> str:
        actions = cls._management_actions(extracted)
        action_type = str(actions[0].get("type") or "").strip().lower() if actions else ""
        return {
            "close": "close_instruction",
            "move_to_break_even": "break_even",
            "edit_stop_loss": "stop_change",
            "edit_take_profit": "take_profit_change",
            "cancel_pending": "cancel",
        }.get(action_type, "provider_update")


__all__ = ["CollectiveAwareCanonicalExecutionDispatcher"]
