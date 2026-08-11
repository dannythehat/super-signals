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


@dataclass(frozen=True, slots=True)
class _ParsedTrade:
    symbol: str
    side: str
    order_type: str
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    take_profits: tuple[Decimal, ...]
    size_multiplier: Decimal
    has_open_runner: bool


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

    @staticmethod
    def _parse_extracted(extracted: dict[str, Any]) -> _ParsedTrade:
        try:
            symbol = str(extracted.get("symbol") or "").strip().upper()
            if symbol == "GOLD":
                symbol = "XAUUSD"
            side = str(extracted.get("side") or "").strip().upper()
            order_type = str(extracted.get("order_type") or "").strip().lower()
            entry_low = _decimal(extracted.get("entry_low"))
            entry_high = _decimal(extracted.get("entry_high"))
            stop_loss = _decimal(extracted.get("stop_loss"))
            take_profits = tuple(
                _decimal(value) for value in (extracted.get("take_profits") or [])
            )
        except ValueError as exc:
            raise ValueError("provider_instruction_incomplete") from exc

        if symbol != "XAUUSD" or side not in {"BUY", "SELL"}:
            raise ValueError("provider_instruction_unsupported")
        if order_type not in {"market", "pending"}:
            raise ValueError("provider_order_type_unsupported")
        if entry_low <= 0 or entry_high <= 0 or stop_loss <= 0:
            raise ValueError("provider_instruction_incomplete")
        if entry_high < entry_low or not take_profits or any(tp <= 0 for tp in take_profits):
            raise ValueError("provider_instruction_incomplete")

        size_multiplier = Decimal("2") if bool(extracted.get("double_lot")) else Decimal("1")
        return _ParsedTrade(
            symbol=symbol,
            side=side,
            order_type=order_type,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
            size_multiplier=size_multiplier,
            has_open_runner=bool(extracted.get("tp_open")),
        )

    @staticmethod
    def _fingerprint(row: Any, trade: _ParsedTrade, revision_index: int) -> str:
        fingerprint_payload = {
            "provider_chat_id": int(row["provider_chat_id"]),
            "provider_message_id": int(row["provider_message_id"]),
            "source_revision_index": revision_index,
            "source_posted_at": _timestamp_token(row["source_posted_at"]),
            "symbol": trade.symbol,
            "side": trade.side,
            "order_type": trade.order_type,
            "entry_low": _token(trade.entry_low),
            "entry_high": _token(trade.entry_high),
            "stop_loss": _token(trade.stop_loss),
            "take_profits": [_token(value) for value in trade.take_profits],
            "has_open_runner": trade.has_open_runner,
            "size_multiplier": _token(trade.size_multiplier),
        }
        return sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()

    def process(
        self,
        *,
        message_id: UUID,
        extracted: dict[str, Any],
        revision_index: int = 0,
    ) -> AiSignalResult:
        try:
            trade = self._parse_extracted(extracted)
        except ValueError as exc:
            return AiSignalResult(False, False, None, str(exc))

        with self._session_factory() as session:
            row = self._message_revision_row(session, message_id, revision_index)
            if row is None:
                return AiSignalResult(False, False, None, "message_not_eligible")

            fingerprint = self._fingerprint(row, trade, revision_index)
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
                        has_open_runner,
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
                        :has_open_runner,
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
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "order_type": trade.order_type,
                    "entry_low": trade.entry_low,
                    "entry_high": trade.entry_high,
                    "stop_loss": trade.stop_loss,
                    "take_profits": json.dumps([_token(value) for value in trade.take_profits]),
                    "has_open_runner": trade.has_open_runner,
                    "risk_multiplier": trade.size_multiplier,
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
                        "order_type": trade.order_type,
                        "entry_is_range": trade.entry_low != trade.entry_high,
                        "has_open_runner": trade.has_open_runner,
                        "position_created": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
            return AiSignalResult(True, False, signal_id, "created")

    def apply_pre_execution_revision(
        self,
        *,
        message_id: UUID,
        extracted: dict[str, Any],
        revision_index: int,
        execute: bool,
        reason: str,
    ) -> AiSignalResult:
        """Replace one canonical signal only while no Day 26 positions exist.

        A Telegram edit is the same logical provider signal. Before execution we use the
        latest edited version. Once execution has started, the signal is immutable here.
        """
        if revision_index <= 0:
            raise ValueError("revision_index_required")

        with self._session_factory() as session:
            existing = session.execute(
                text(
                    """
                    SELECT id
                    FROM signals
                    WHERE source_message_id = :message_id
                    FOR UPDATE
                    """
                ),
                {"message_id": message_id},
            ).scalar_one_or_none()

            if existing is None:
                pass
            else:
                execution_started = bool(
                    session.execute(
                        text(
                            """
                            SELECT EXISTS(
                                SELECT 1
                                FROM positions
                                WHERE signal_id = :signal_id
                            )
                            """
                        ),
                        {"signal_id": existing},
                    ).scalar_one()
                )
                if execution_started:
                    return AiSignalResult(False, False, existing, "post_execution_edit")

                row = self._message_revision_row(session, message_id, revision_index)
                if row is None:
                    return AiSignalResult(False, False, existing, "message_not_eligible")

                if not execute:
                    skip_fingerprint = sha256(
                        (
                            f"revision-skip:{existing}:{revision_index}:"
                            f"{str(row['original_text'] or '')}"
                        ).encode("utf-8")
                    ).hexdigest()
                    session.execute(
                        text(
                            """
                            UPDATE signals
                            SET source_revision_index = :revision_index,
                                source_posted_at = :source_posted_at,
                                parser_status = 'skipped',
                                skip_reason = :skip_reason,
                                original_text = :original_text,
                                updated_at = now()
                            WHERE id = :signal_id
                            """
                        ),
                        {
                            "signal_id": existing,
                            "revision_index": revision_index,
                            "source_posted_at": row["source_posted_at"],
                            "skip_reason": reason[:500],
                            "original_text": str(row["original_text"] or ""),
                        },
                    )
                    self._observe(
                        session,
                        signal_id=existing,
                        message_id=message_id,
                        revision_index=revision_index,
                        fingerprint=skip_fingerprint,
                        disposition="canonical",
                    )
                    session.add(
                        AuditEvent(
                            actor_user_id=None,
                            event_type="signal.revised_before_execution.ai_supervisor",
                            entity_type="signal",
                            entity_id=existing,
                            payload={
                                "source_revision_index": revision_index,
                                "accepted": False,
                                "skip_reason": reason,
                                "trade_action_created": False,
                            },
                        )
                    )
                    session.commit()
                    return AiSignalResult(False, False, existing, "revision_skipped")

                try:
                    trade = self._parse_extracted(extracted)
                except ValueError as exc:
                    session.execute(
                        text(
                            """
                            UPDATE signals
                            SET source_revision_index = :revision_index,
                                source_posted_at = :source_posted_at,
                                parser_status = 'skipped',
                                skip_reason = :skip_reason,
                                original_text = :original_text,
                                updated_at = now()
                            WHERE id = :signal_id
                            """
                        ),
                        {
                            "signal_id": existing,
                            "revision_index": revision_index,
                            "source_posted_at": row["source_posted_at"],
                            "skip_reason": str(exc)[:500],
                            "original_text": str(row["original_text"] or ""),
                        },
                    )
                    session.commit()
                    return AiSignalResult(False, False, existing, str(exc))

                fingerprint = self._fingerprint(row, trade, revision_index)
                session.execute(
                    text(
                        """
                        UPDATE signals
                        SET source_revision_index = :revision_index,
                            source_posted_at = :source_posted_at,
                            signal_fingerprint = :fingerprint,
                            symbol = :symbol,
                            side = :side,
                            order_type = :order_type,
                            entry_low = :entry_low,
                            entry_high = :entry_high,
                            stop_loss = :stop_loss,
                            take_profits = CAST(:take_profits AS jsonb),
                            has_open_runner = :has_open_runner,
                            parser_status = 'accepted',
                            skip_reason = NULL,
                            risk_multiplier = :risk_multiplier,
                            original_text = :original_text,
                            updated_at = now()
                        WHERE id = :signal_id
                        """
                    ),
                    {
                        "signal_id": existing,
                        "revision_index": revision_index,
                        "source_posted_at": row["source_posted_at"],
                        "fingerprint": fingerprint,
                        "symbol": trade.symbol,
                        "side": trade.side,
                        "order_type": trade.order_type,
                        "entry_low": trade.entry_low,
                        "entry_high": trade.entry_high,
                        "stop_loss": trade.stop_loss,
                        "take_profits": json.dumps(
                            [_token(value) for value in trade.take_profits]
                        ),
                        "has_open_runner": trade.has_open_runner,
                        "risk_multiplier": trade.size_multiplier,
                        "original_text": str(row["original_text"] or ""),
                    },
                )
                self._observe(
                    session,
                    signal_id=existing,
                    message_id=message_id,
                    revision_index=revision_index,
                    fingerprint=fingerprint,
                    disposition="canonical",
                )
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_type="signal.revised_before_execution.ai_supervisor",
                        entity_type="signal",
                        entity_id=existing,
                        payload={
                            "source_revision_index": revision_index,
                            "accepted": True,
                            "entry_is_range": trade.entry_low != trade.entry_high,
                            "has_open_runner": trade.has_open_runner,
                            "trade_action_created": False,
                        },
                    )
                )
                session.commit()
                return AiSignalResult(False, False, existing, "revision_applied")

        if execute:
            return self.process(
                message_id=message_id,
                extracted=extracted,
                revision_index=revision_index,
            )
        return AiSignalResult(False, False, None, "no_existing_signal")

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
