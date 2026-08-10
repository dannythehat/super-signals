"""Day 16 append-only parser evidence for classified XAUUSD new trades."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent, Message
from app.xauusd_parser import PARSER_VERSION, ParseResult, parse_xauusd_trade


class MessageParserService:
    """Parse only Day 15 evidence that is explicitly New Trade / classified."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def parse_original(self, source_id: UUID, telegram_message_id: int) -> bool:
        with self._session_factory() as session:
            message = session.scalar(
                select(Message).where(
                    Message.source_id == source_id,
                    Message.telegram_message_id == telegram_message_id,
                )
            )
            if message is None or message.deleted_at is not None:
                return False
            inserted = self._parse_revision_if_eligible(
                session,
                message_id=message.id,
                revision_index=0,
                raw_text=message.raw_text,
            )
            session.commit()
            return inserted

    def parse_latest_revision(self, source_id: UUID, telegram_message_id: int) -> bool:
        with self._session_factory() as session:
            message = session.scalar(
                select(Message).where(
                    Message.source_id == source_id,
                    Message.telegram_message_id == telegram_message_id,
                )
            )
            if message is None or message.deleted_at is not None:
                return False
            revision = session.execute(
                text(
                    """
                    SELECT revision_index, raw_text
                    FROM message_revisions
                    WHERE message_id = :message_id
                    ORDER BY revision_index DESC
                    LIMIT 1
                    """
                ),
                {"message_id": message.id},
            ).mappings().first()
            if revision is None:
                return False
            inserted = self._parse_revision_if_eligible(
                session,
                message_id=message.id,
                revision_index=int(revision["revision_index"]),
                raw_text=str(revision["raw_text"] or ""),
            )
            session.commit()
            return inserted

    def backfill_eligible(self) -> int:
        """Parse any eligible New Trade classifications not yet attempted."""

        inserted_count = 0
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        mc.message_id,
                        mc.revision_index,
                        CASE
                            WHEN mc.revision_index = 0 THEN m.raw_text
                            ELSE mr.raw_text
                        END AS raw_text
                    FROM message_classifications AS mc
                    JOIN messages AS m ON m.id = mc.message_id
                    LEFT JOIN message_revisions AS mr
                      ON mr.message_id = mc.message_id
                     AND mr.revision_index = mc.revision_index
                    LEFT JOIN message_parses AS mp
                      ON mp.message_id = mc.message_id
                     AND mp.revision_index = mc.revision_index
                    WHERE m.deleted_at IS NULL
                      AND mc.classification = 'new_trade'
                      AND mc.decision_status = 'classified'
                      AND mp.id IS NULL
                    ORDER BY mc.created_at ASC, mc.revision_index ASC
                    """
                )
            ).mappings().all()
            for row in rows:
                if self._insert_parse(
                    session,
                    message_id=row["message_id"],
                    revision_index=int(row["revision_index"]),
                    raw_text=str(row["raw_text"] or ""),
                ):
                    inserted_count += 1
            session.commit()
        return inserted_count

    @staticmethod
    def _eligible(session: Session, message_id: UUID, revision_index: int) -> bool:
        row = session.execute(
            text(
                """
                SELECT classification, decision_status
                FROM message_classifications
                WHERE message_id = :message_id
                  AND revision_index = :revision_index
                """
            ),
            {"message_id": message_id, "revision_index": revision_index},
        ).mappings().first()
        return bool(
            row
            and row["classification"] == "new_trade"
            and row["decision_status"] == "classified"
        )

    def _parse_revision_if_eligible(
        self,
        session: Session,
        *,
        message_id: UUID,
        revision_index: int,
        raw_text: str,
    ) -> bool:
        if not self._eligible(session, message_id, revision_index):
            return False
        return self._insert_parse(
            session,
            message_id=message_id,
            revision_index=revision_index,
            raw_text=raw_text,
        )

    @staticmethod
    def _insert_parse(
        session: Session,
        *,
        message_id: UUID,
        revision_index: int,
        raw_text: str,
    ) -> bool:
        result = parse_xauusd_trade(raw_text)
        trade = result.trade
        inserted_id = session.execute(
            text(
                """
                INSERT INTO message_parses (
                    message_id,
                    revision_index,
                    parse_status,
                    reason,
                    matched_rules,
                    parsed_text_sha256,
                    parser_version,
                    symbol,
                    direction,
                    entry_price,
                    stop_loss,
                    take_profits,
                    size_multiplier
                )
                VALUES (
                    :message_id,
                    :revision_index,
                    :parse_status,
                    :reason,
                    CAST(:matched_rules AS jsonb),
                    :parsed_text_sha256,
                    :parser_version,
                    :symbol,
                    :direction,
                    :entry_price,
                    :stop_loss,
                    CAST(:take_profits AS jsonb),
                    :size_multiplier
                )
                ON CONFLICT (message_id, revision_index) DO NOTHING
                RETURNING id
                """
            ),
            {
                "message_id": message_id,
                "revision_index": revision_index,
                "parse_status": result.status,
                "reason": result.reason,
                "matched_rules": json.dumps(list(result.matched_rules)),
                "parsed_text_sha256": sha256(raw_text.encode("utf-8")).hexdigest(),
                "parser_version": PARSER_VERSION,
                "symbol": trade.symbol if trade else None,
                "direction": trade.direction if trade else None,
                "entry_price": trade.entry if trade else None,
                "stop_loss": trade.stop_loss if trade else None,
                "take_profits": json.dumps([str(value) for value in trade.take_profits] if trade else []),
                "size_multiplier": trade.size_multiplier if trade else None,
            },
        ).scalar_one_or_none()
        if inserted_id is None:
            return False

        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="message.parsed",
                entity_type="message",
                entity_id=message_id,
                payload={
                    "revision_index": revision_index,
                    "parse_status": result.status,
                    "matched_rules": list(result.matched_rules),
                    "parser_version": PARSER_VERSION,
                    "signal_created": False,
                    "position_created": False,
                    "lot_size_calculated": False,
                    "trade_action_created": False,
                },
            )
        )
        return True


async def backfill_parser_service(service: MessageParserService) -> int:
    """Small async helper for listener startup."""

    return await asyncio.to_thread(service.backfill_eligible)
