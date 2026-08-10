"""Day 18 canonical Signal creation and duplicate protection.

Only revision-0 provider messages that passed Day 15 classification, Day 16 parsing
and Day 17 strict validation may create a Signal. No Position, risk calculation,
publishing or broker action occurs here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent, Message

SIGNAL_EVENT_VERSION = "day18-signal-v1"


@dataclass(frozen=True, slots=True)
class SignalBackfillResult:
    signals_created: int
    duplicate_observations: int


@dataclass(frozen=True, slots=True)
class SignalCreateResult:
    created: bool
    duplicate: bool
    signal_id: UUID | None
    fingerprint: str | None


def _decimal_token(value: Any) -> str:
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid decimal signal value: {value!r}") from exc
    if not decimal_value.is_finite():
        raise ValueError("Signal decimal values must be finite.")
    normalized = decimal_value.normalize()
    token = format(normalized, "f")
    return "0" if token in {"-0", "-0.0"} else token


def _timestamp_token(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def build_signal_fingerprint(
    *,
    provider_chat_id: int,
    provider_message_id: int,
    source_posted_at: datetime,
    symbol: str,
    side: str,
    entry_price: Any,
    stop_loss: Any,
    take_profits: list[Any] | tuple[Any, ...],
    size_multiplier: Any,
    order_type: str = "market",
) -> str:
    """Return the deterministic internal fingerprint for one provider signal."""

    payload = {
        "provider_chat_id": int(provider_chat_id),
        "provider_message_id": int(provider_message_id),
        "source_posted_at": _timestamp_token(source_posted_at),
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "order_type": str(order_type).lower(),
        "entry": _decimal_token(entry_price),
        "stop_loss": _decimal_token(stop_loss),
        "take_profits": [_decimal_token(value) for value in take_profits],
        "size_multiplier": _decimal_token(size_multiplier),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


class CanonicalSignalService:
    """Create one canonical Signal for each accepted provider message."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def process_original(self, source_id: UUID, telegram_message_id: int) -> SignalCreateResult:
        with self._session_factory() as session:
            message = session.scalar(
                select(Message).where(
                    Message.source_id == source_id,
                    Message.telegram_message_id == telegram_message_id,
                )
            )
            if message is None or message.deleted_at is not None:
                return SignalCreateResult(False, False, None, None)
            row = self._accepted_revision_zero_row(session, message.id)
            if row is None:
                return SignalCreateResult(False, False, None, None)
            result = self._create_or_observe(session, row)
            session.commit()
            return result

    def backfill(self) -> SignalBackfillResult:
        signals_created = 0
        duplicate_observations = 0
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        m.id AS message_id,
                        m.source_id,
                        s.chat_id AS provider_chat_id,
                        m.telegram_message_id AS provider_message_id,
                        m.posted_at AS source_posted_at,
                        m.raw_text AS original_text,
                        mp.symbol,
                        mp.direction,
                        mp.entry_price,
                        mp.stop_loss,
                        mp.take_profits,
                        mp.size_multiplier
                    FROM messages AS m
                    JOIN sources AS s ON s.id = m.source_id
                    JOIN message_classifications AS mc
                      ON mc.message_id = m.id
                     AND mc.revision_index = 0
                    JOIN message_parses AS mp
                      ON mp.message_id = m.id
                     AND mp.revision_index = 0
                    JOIN message_validations AS mv
                      ON mv.message_id = m.id
                     AND mv.revision_index = 0
                    WHERE m.deleted_at IS NULL
                      AND mc.classification = 'new_trade'
                      AND mc.decision_status = 'classified'
                      AND mp.parse_status = 'parsed'
                      AND mv.validation_status = 'valid'
                    ORDER BY m.posted_at ASC, m.telegram_message_id ASC
                    """
                )
            ).mappings().all()
            for row in rows:
                result = self._create_or_observe(session, row)
                if result.created:
                    signals_created += 1
                elif result.duplicate:
                    duplicate_observations += 1
            session.commit()
        return SignalBackfillResult(signals_created, duplicate_observations)

    @staticmethod
    def _accepted_revision_zero_row(session: Session, message_id: UUID) -> Any | None:
        return session.execute(
            text(
                """
                SELECT
                    m.id AS message_id,
                    m.source_id,
                    s.chat_id AS provider_chat_id,
                    m.telegram_message_id AS provider_message_id,
                    m.posted_at AS source_posted_at,
                    m.raw_text AS original_text,
                    mp.symbol,
                    mp.direction,
                    mp.entry_price,
                    mp.stop_loss,
                    mp.take_profits,
                    mp.size_multiplier
                FROM messages AS m
                JOIN sources AS s ON s.id = m.source_id
                JOIN message_classifications AS mc
                  ON mc.message_id = m.id
                 AND mc.revision_index = 0
                JOIN message_parses AS mp
                  ON mp.message_id = m.id
                 AND mp.revision_index = 0
                JOIN message_validations AS mv
                  ON mv.message_id = m.id
                 AND mv.revision_index = 0
                WHERE m.id = :message_id
                  AND m.deleted_at IS NULL
                  AND mc.classification = 'new_trade'
                  AND mc.decision_status = 'classified'
                  AND mp.parse_status = 'parsed'
                  AND mv.validation_status = 'valid'
                """
            ),
            {"message_id": message_id},
        ).mappings().first()

    def _create_or_observe(self, session: Session, row: Any) -> SignalCreateResult:
        take_profits = [str(item) for item in (row["take_profits"] or [])]
        fingerprint = build_signal_fingerprint(
            provider_chat_id=int(row["provider_chat_id"]),
            provider_message_id=int(row["provider_message_id"]),
            source_posted_at=row["source_posted_at"],
            symbol=str(row["symbol"]),
            side=str(row["direction"]),
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            take_profits=take_profits,
            size_multiplier=row["size_multiplier"],
        )

        existing = session.execute(
            text(
                """
                SELECT id, source_message_id, signal_fingerprint
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
            observation_inserted = self._insert_observation(
                session,
                signal_id=existing["id"],
                message_id=row["message_id"],
                fingerprint=fingerprint,
                disposition="canonical" if same_message else "duplicate",
            )
            if observation_inserted and not same_message:
                self._audit_duplicate(session, existing["id"], row["message_id"], fingerprint)
            return SignalCreateResult(False, not same_message, existing["id"], fingerprint)

        inserted_id = session.execute(
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
                    0,
                    :source_posted_at,
                    :signal_fingerprint,
                    :symbol,
                    :side,
                    'market',
                    :entry_price,
                    :entry_price,
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
                "source_posted_at": row["source_posted_at"],
                "signal_fingerprint": fingerprint,
                "symbol": str(row["symbol"]),
                "side": str(row["direction"]),
                "entry_price": row["entry_price"],
                "stop_loss": row["stop_loss"],
                "take_profits": json.dumps(take_profits),
                "risk_multiplier": row["size_multiplier"],
                "original_text": str(row["original_text"] or ""),
            },
        ).scalar_one_or_none()

        if inserted_id is None:
            # A concurrent observer won one of the unique constraints. Resolve it
            # to the canonical event rather than retrying another insert.
            winner = session.execute(
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
            if winner is None:
                return SignalCreateResult(False, False, None, fingerprint)
            same_message = winner["source_message_id"] == row["message_id"]
            observation_inserted = self._insert_observation(
                session,
                signal_id=winner["id"],
                message_id=row["message_id"],
                fingerprint=fingerprint,
                disposition="canonical" if same_message else "duplicate",
            )
            if observation_inserted and not same_message:
                self._audit_duplicate(session, winner["id"], row["message_id"], fingerprint)
            return SignalCreateResult(False, not same_message, winner["id"], fingerprint)

        self._insert_observation(
            session,
            signal_id=inserted_id,
            message_id=row["message_id"],
            fingerprint=fingerprint,
            disposition="canonical",
        )
        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="signal.created",
                entity_type="signal",
                entity_id=inserted_id,
                payload={
                    "signal_event_version": SIGNAL_EVENT_VERSION,
                    "source_message_id": str(row["message_id"]),
                    "provider_message_id": int(row["provider_message_id"]),
                    "signal_fingerprint": fingerprint,
                    "source_revision_index": 0,
                    "position_created": False,
                    "lot_size_calculated": False,
                    "published": False,
                    "trade_action_created": False,
                },
            )
        )
        return SignalCreateResult(True, False, inserted_id, fingerprint)

    @staticmethod
    def _insert_observation(
        session: Session,
        *,
        signal_id: UUID,
        message_id: UUID,
        fingerprint: str,
        disposition: str,
    ) -> bool:
        inserted = session.execute(
            text(
                """
                INSERT INTO signal_observations (
                    signal_id,
                    message_id,
                    revision_index,
                    disposition,
                    observed_fingerprint
                )
                VALUES (
                    :signal_id,
                    :message_id,
                    0,
                    :disposition,
                    :observed_fingerprint
                )
                ON CONFLICT (message_id, revision_index) DO NOTHING
                RETURNING id
                """
            ),
            {
                "signal_id": signal_id,
                "message_id": message_id,
                "disposition": disposition,
                "observed_fingerprint": fingerprint,
            },
        ).scalar_one_or_none()
        return inserted is not None

    @staticmethod
    def _audit_duplicate(session: Session, signal_id: UUID, message_id: UUID, fingerprint: str) -> None:
        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="signal.duplicate_ignored",
                entity_type="signal",
                entity_id=signal_id,
                payload={
                    "signal_event_version": SIGNAL_EVENT_VERSION,
                    "duplicate_message_id": str(message_id),
                    "signal_fingerprint": fingerprint,
                    "position_created": False,
                    "published": False,
                    "trade_action_created": False,
                },
            )
        )
