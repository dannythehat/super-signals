"""Day 20 canonical lifecycle events for one logical Signal thread.

Provider updates are linked only by an explicit Telegram reply to the original
provider signal in the same logical source. The stored event text is rendered
from supported lifecycle rules rather than copied from provider wording, so the
member-facing mirror cannot expose provider identity accidentally.

This module creates no Position, performs no broker action and calculates no
profit or pips. Later trading days may append broker-backed lifecycle events to
the same ledger.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent

LIFECYCLE_VERSION = "day20-lifecycle-v1"


@dataclass(frozen=True, slots=True)
class RenderedLifecycleUpdate:
    event_type: str
    text: str


@dataclass(frozen=True, slots=True)
class LifecycleCreateResult:
    created: bool
    linked: bool
    signal_id: UUID | None
    event_id: UUID | None
    event_type: str | None
    reason: str


_TP_NUMBER = re.compile(r"\b(?:TP|TARGET)\s*#?\s*(\d+)\b", re.IGNORECASE)
_STOP_VALUE = re.compile(
    r"\b(?:MOVE\s+)?(?:SL|STOP(?:\s+LOSS)?)\s+(?:TO\s+)?(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_CLOSE_PERCENT = re.compile(r"\b(\d{1,3})\s*%\b")


def _decimal_text(value: str) -> str:
    return format(Decimal(value).normalize(), "f")


def render_provider_update(
    raw_text: str,
    matched_rules: list[str] | tuple[str, ...],
) -> RenderedLifecycleUpdate | None:
    """Render a supported provider update without copying arbitrary provider text."""

    rules = set(matched_rules)
    lines = ["TRADE UPDATE"]
    event_type: str | None = None

    if "target_hit" in rules:
        target = _TP_NUMBER.search(raw_text or "")
        lines.append(
            f"TP{target.group(1)} reached."
            if target is not None
            else "Take-profit target reached."
        )
        event_type = "take_profit_hit"
    elif "stop_hit" in rules:
        lines.append("Stop loss reached.")
        event_type = "stop_loss_hit"
    elif "cancel_order" in rules:
        lines.append("Pending order cancellation instructed.")
        event_type = "cancel"
    elif "break_even" in rules:
        lines.append("SL moved to entry — trade remains open with break-even protection.")
        event_type = "break_even"
    elif "move_stop" in rules:
        stop_value = _STOP_VALUE.search(raw_text or "")
        if stop_value is not None:
            lines.append(f"Stop loss changed to {_decimal_text(stop_value.group(1))}.")
        else:
            lines.append("Stop loss change instructed.")
        event_type = "stop_change"
    elif "secure_profit" in rules:
        lines.append("Profit-secure instruction received.")
        event_type = "partial_close"
    elif "close_trade" in rules:
        normalised = " ".join((raw_text or "").split())
        partial = bool(
            re.search(r"\b(?:PARTIAL|HALF)\b", normalised, re.IGNORECASE)
            or _CLOSE_PERCENT.search(normalised)
        )
        if partial:
            percent = _CLOSE_PERCENT.search(normalised)
            lines.append(
                f"Partial close of {percent.group(1)}% instructed."
                if percent is not None
                else "Partial close instructed."
            )
            event_type = "partial_close"
        else:
            target = _TP_NUMBER.search(raw_text or "")
            lines.append(
                f"Separate close request received for TP{target.group(1)}."
                if target is not None
                else "A separate close request was received."
            )
            lines.append("This is not a broker closure confirmation.")
            event_type = "close_instruction"
    elif "hold_existing_trade" in rules:
        lines.append("Trade remains active.")
        event_type = "hold"
    else:
        return None

    if "break_even" in rules and event_type != "break_even":
        lines.append("SL moved to entry — trade remains open with break-even protection.")
    elif "move_stop" in rules and event_type not in {"stop_change", "break_even"}:
        stop_value = _STOP_VALUE.search(raw_text or "")
        lines.append(
            f"Stop loss changed to {_decimal_text(stop_value.group(1))}."
            if stop_value is not None
            else "Stop loss change instructed."
        )

    return RenderedLifecycleUpdate(event_type=event_type, text="\n".join(lines))


class SignalLifecycleService:
    """Create idempotent stored lifecycle events from explicit linked updates."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def process_original(
        self,
        source_id: UUID,
        telegram_message_id: int,
    ) -> LifecycleCreateResult:
        with self._session_factory() as session:
            row = self._update_row(
                session,
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=0,
            )
            if row is None:
                return LifecycleCreateResult(
                    False, False, None, None, None, "Message is not a classified trade update."
                )
            result = self._create_from_row(session, row, audit_unlinked=True)
            session.commit()
            return result

    def process_latest_revision(
        self,
        source_id: UUID,
        telegram_message_id: int,
    ) -> LifecycleCreateResult:
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
                return LifecycleCreateResult(
                    False, False, None, None, None, "Message was not found."
                )
            row = self._update_row(
                session,
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=int(revision_index),
            )
            if row is None:
                return LifecycleCreateResult(
                    False, False, None, None, None, "Revision is not a classified trade update."
                )
            result = self._create_from_row(session, row, audit_unlinked=True)
            session.commit()
            return result

    def backfill(self) -> int:
        """Recover safely linked update events after restart without duplicate rows."""

        created = 0
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
                      AND (
                          mc.revision_index = 0
                          OR mr.revision_index IS NOT NULL
                      )
                    ORDER BY m.posted_at ASC, mc.revision_index ASC
                    """
                )
            ).mappings().all()
            for row in rows:
                if self._create_from_row(session, row, audit_unlinked=False).created:
                    created += 1
            session.commit()
        return created

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

    def _create_from_row(
        self,
        session: Session,
        row: Any,
        *,
        audit_unlinked: bool,
    ) -> LifecycleCreateResult:
        reply_to_message_id = self._reply_id(row["raw_payload"])
        if reply_to_message_id is None:
            if audit_unlinked:
                self._audit_stopped(
                    session,
                    row=row,
                    reason="Trade update has no explicit reply link to a canonical Signal.",
                )
            return LifecycleCreateResult(
                False,
                False,
                None,
                None,
                None,
                "Trade update has no explicit reply link to a canonical Signal.",
            )

        signal = session.execute(
            text(
                """
                SELECT id
                FROM signals
                WHERE source_id = :source_id
                  AND provider_message_id = :provider_message_id
                LIMIT 1
                """
            ),
            {
                "source_id": row["source_id"],
                "provider_message_id": reply_to_message_id,
            },
        ).mappings().first()
        if signal is None:
            if audit_unlinked:
                self._audit_stopped(
                    session,
                    row=row,
                    reason="Reply target does not resolve to a canonical Signal in this source.",
                )
            return LifecycleCreateResult(
                False,
                False,
                None,
                None,
                None,
                "Reply target does not resolve to a canonical Signal in this source.",
            )

        rules = self._rules(row["matched_rules"])
        rendered = render_provider_update(str(row["raw_text"] or ""), rules)
        if rendered is None:
            if audit_unlinked:
                self._audit_stopped(
                    session,
                    row=row,
                    reason="Trade update contains no supported Day 20 lifecycle action.",
                )
            return LifecycleCreateResult(
                False,
                True,
                signal["id"],
                None,
                None,
                "Trade update contains no supported Day 20 lifecycle action.",
            )

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
            existing = session.execute(
                text("SELECT id FROM signal_lifecycle_events WHERE event_key = :event_key"),
                {"event_key": event_key},
            ).scalar_one_or_none()
            return LifecycleCreateResult(
                False,
                True,
                signal["id"],
                existing,
                rendered.event_type,
                "Lifecycle event already exists.",
            )

        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="signal.lifecycle_event_created",
                entity_type="signal",
                entity_id=signal["id"],
                payload={
                    "lifecycle_version": LIFECYCLE_VERSION,
                    "lifecycle_event_id": str(event_id),
                    "event_type": rendered.event_type,
                    "source_message_id": str(row["message_id"]),
                    "source_revision_index": int(row["revision_index"]),
                    "provider_identity_exposed": False,
                    "position_created": False,
                    "trade_action_created": False,
                    "pips_calculated": False,
                },
            )
        )
        return LifecycleCreateResult(
            True,
            True,
            signal["id"],
            event_id,
            rendered.event_type,
            "Lifecycle event created.",
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
                    "lifecycle_version": LIFECYCLE_VERSION,
                    "source_revision_index": int(row["revision_index"]),
                    "reason": reason,
                    "signal_changed": False,
                    "position_created": False,
                    "trade_action_created": False,
                    "published": False,
                },
            )
        )
