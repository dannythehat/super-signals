"""Promote an AI-understood provider trade into the canonical Signal ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent

AI_SIGNAL_VERSION = "ai-supervisor-signal-v1"


@dataclass(frozen=True, slots=True)
class AiSignalResult:
    created: bool
    duplicate: bool
    signal_id: UUID | None
    reason: str


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("signal_numeric_value_invalid") from exc
    if not parsed.is_finite():
        raise ValueError("signal_numeric_value_invalid")
    return parsed


def _token(value: Decimal) -> str:
    normal = value.normalize()
    result = format(normal, "f")
    return "0" if result in {"-0", "-0.0"} else result


def _timestamp_token(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class AiCanonicalSignalService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def process(
        self,
        *,
        message_id: UUID,
        extracted: dict[str, Any],
        revision_index: int = 0,
    ) -> AiSignalResult:
        try:
            symbol = str(extracted.get("symbol") or "").strip().upper()
            if symbol == "GOLD":
                symbol = "XAUUSD"
            side = str(extracted.get("side") or "").strip().upper()
            order_type = str(extracted.get("order_type") or "").strip().lower()
            entry_low = _decimal(extracted.get("entry_low"))
            entry_high = _decimal(extracted.get("entry_high"))
            stop_loss = _decimal(extracted.get("stop_loss"))
            take_profits = [_decimal(value) for value in (extracted.get("take_profits") or [])]
        except ValueError:
            return AiSignalResult(False, False, None, "provider_instruction_incomplete")

        if symbol != "XAUUSD" or side not in {"BUY", "SELL"}:
            return AiSignalResult(False, False, None, "provider_instruction_unsupported")
        if order_type not in {"market", "pending"}:
            return AiSignalResult(False, False, None, "provider_order_type_unsupported")
        if entry_high < entry_low or not take_profits:
            return AiSignalResult(False, False, None, "provider_instruction_incomplete")

        size_multiplier = Decimal("2") if bool(extracted.get("double_lot")) else Decimal("1")

        with self._session_factory() as session:
            row = self._message_revision_row(session, message_id, revision_index)
            if row is None:
                return AiSignalResult(False, False, None, "message_not_eligible")

            fingerprint_payload = {
                "provider_chat_id": int(row["provider_chat_id"]),
                "provider_message_id": int(row["provider_message_id"]),
                "source_revision_index": revision_index,
                "source_posted_at": _timestamp_token(row["source_posted_at"]),
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "entry_low": _token(entry_low),
                "entry_high": _token(entry_high),
                "stop_loss": _token(stop_loss),
                "take_profits": [_token(value) for value in take_profits],
                "size_multiplier": _token(size_multiplier),
            }
            fingerprint = sha256(
                json.dumps(
                    fingerprint_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest()

            existing = session.execute(
                text(
                    """
                    SELECT id, source_message_id
                    FROM signals
                    WHERE source_message_id = :message_id
                       OR signal_fingerprint = :fingerprint
                       OR (
                            provider_chat_id = :provider_chat_id
                            AND provider_message_id = :provider_message_id
                       )
                    ORDER BY created_at ASC
                    LIMIT 1
                    """
                ),
                {
                    "message_id": row["message_id"],
                    "fingerprint": fingerprint,
                    "provider_chat_id": int(row["provider_chat_id"]),
                    "provider_message_id": int(row["provider_message_id"]),
                },
            ).mappings().first()
            if existing is not None:
                same_message = existing["source_message_id"] == row["message_id"]
                self._observe(
                    session,
                    signal_id=existing["id"],
                    message_id=row["message_id"],
                    revision_index=revision_index,
                    fingerprint=fingerprint,
                    disposition="canonical" if same_message else "duplicate",
                )
                session.commit()
                return AiSignalResult(False, not same_message, existing["id"], "existing_signal")

            signal_id = session.execute(
                text(
                    """
                    INSERT INTO signals (
                        source_message_id,
                        source_id,
                        provider_chat_id,
                        provider_message_id,
                        source_revision_index,
                        source_posted_at,
                        signal_fingerprint,
                        symbol,
                        side,
                        order_type,
                        entry_low,
                        entry_high,
                        stop_loss,
                        take_profits,
                        parser_status,
                        skip_reason,
                        risk_multiplier,
                        original_text
                    )
                    VALUES (
                        :source_message_id,
                        :source_id,
                        :provider_chat_id,
                        :provider_message_id,
                        :source_revision_index,
                        :source_posted_at,
                        :fingerprint,
                        :symbol,
                        :side,
                        :order_type,
                        :entry_low,
                        :entry_high,
                        :stop_loss,
                        CAST(:take_profits AS jsonb),
                        'accepted',
                        NULL,
                        :risk_multiplier,
                        :original_text
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "source_message_id": row["message_id"],
                    "source_id": row["source_id"],
                    "provider_chat_id": int(row["provider_chat_id"]),
                    "provider_message_id": int(row["provider_message_id"]),
                    "source_revision_index": revision_index,
                    "source_posted_at": row["source_posted_at"],
                    "fingerprint": fingerprint,
                    "symbol": symbol,
                    "side": side,
                    "order_type": order_type,
                    "entry_low": entry_low,
                    "entry_high": entry_high,
                    "stop_loss": stop_loss,
                    "take_profits": json.dumps([_token(value) for value in take_profits]),
                    "risk_multiplier": size_multiplier,
                    "original_text": str(row["original_text"] or ""),
                },
            ).scalar_one_or_none()
            if signal_id is None:
                session.rollback()
                return AiSignalResult(False, True, None, "concurrent_duplicate")

            self._observe(
                session,
                signal_id=signal_id,
                message_id=row["message_id"],
                revision_index=revision_index,
                fingerprint=fingerprint,
                disposition="canonical",
            )
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="signal.created.ai_supervisor",
                    entity_type="signal",
                    entity_id=signal_id,
                    payload={
                        "signal_event_version": AI_SIGNAL_VERSION,
                        "source_message_id": str(row["message_id"]),
                        "provider_message_id": int(row["provider_message_id"]),
                        "source_revision_index": revision_index,
                        "order_type": order_type,
                        "entry_is_range": entry_low != entry_high,
                        "position_created": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
            return AiSignalResult(True, False, signal_id, "created")

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
                  AND s.status IN ('testing', 'live')
                  AND (:revision_index = 0 OR mr.revision_index IS NOT NULL)
                """
            ),
            {"message_id": message_id, "revision_index": revision_index},
        ).mappings().first()

    @staticmethod
    def _observe(
        session: Session,
        *,
        signal_id: UUID,
        message_id: UUID,
        revision_index: int,
        fingerprint: str,
        disposition: str,
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO signal_observations (
                    signal_id,
                    message_id,
                    revision_index,
                    disposition,
                    observed_fingerprint
                )
                VALUES (:signal_id, :message_id, :revision_index, :disposition, :fingerprint)
                ON CONFLICT (message_id, revision_index) DO NOTHING
                """
            ),
            {
                "signal_id": signal_id,
                "message_id": message_id,
                "revision_index": revision_index,
                "disposition": disposition,
                "fingerprint": fingerprint,
            },
        )
