"""Append-only persistence for the real-source parser v2."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.message_parser import MessageParserService
from app.models import AuditEvent
from app.xauusd_parser_v2 import PARSER_VERSION_V2, parse_xauusd_trade_v2


class MessageParserServiceV2(MessageParserService):
    """Use parser v2 for new eligible messages without mutating old evidence."""

    @staticmethod
    def _insert_parse(
        session: Any,
        *,
        message_id: UUID,
        revision_index: int,
        raw_text: str,
    ) -> bool:
        result = parse_xauusd_trade_v2(raw_text)
        trade = result.trade
        exact_entry = (
            trade.entry_low
            if trade is not None and trade.entry_low == trade.entry_high
            else None
        )
        take_profits = []
        if trade is not None:
            take_profits = [
                str(value) if value is not None else None
                for value in trade.take_profits
            ]

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
                    entry_low,
                    entry_high,
                    order_type,
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
                    :entry_low,
                    :entry_high,
                    :order_type,
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
                "parser_version": PARSER_VERSION_V2,
                "symbol": trade.symbol if trade else None,
                "direction": trade.direction if trade else None,
                "entry_price": exact_entry,
                "entry_low": trade.entry_low if trade else None,
                "entry_high": trade.entry_high if trade else None,
                "order_type": trade.order_type if trade else None,
                "stop_loss": trade.stop_loss if trade else None,
                "take_profits": json.dumps(take_profits),
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
                    "parser_version": PARSER_VERSION_V2,
                    "entry_range_preserved": bool(
                        trade is not None and trade.entry_low != trade.entry_high
                    ),
                    "order_type": trade.order_type if trade else None,
                    "open_runner_preserved": bool(
                        trade is not None and any(value is None for value in trade.take_profits)
                    ),
                    "signal_created": False,
                    "position_created": False,
                    "lot_size_calculated": False,
                    "trade_action_created": False,
                },
            )
        )
        return True
