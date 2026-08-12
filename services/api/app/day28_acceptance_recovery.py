"""Acceptance-only recovery for the Day 28 same-account Telegram harness.

The one-shot sender uses the same Telegram identity as the reader. Telegram did not
fan the sender's outgoing ``Close all`` reply back to the already-running reader, so
that reply was not a live event. On the next bounded catch-up the normal Day 21 AI/V1
pipeline will persist and classify the real Telegram reply, but Day 28 deliberately
never executes catch-up messages.

This helper bridges only that explicitly configured acceptance reply after catch-up. It
loads the already-stored decision/lifecycle event and invokes the normal Day 28
router; it does not invent or insert a management instruction.
"""

from __future__ import annotations

import asyncio
import logging
import os
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.day28_full_execution import Day28FullExecutionRouter
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.mt5_management_day27 import Day27Mt5ManagementService

logger = logging.getLogger(__name__)


def _cipher() -> MetaApiTokenCipher:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    keys = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not keys:
        raise RuntimeError("day28_recovery_broker_keys_missing")
    return MetaApiTokenCipher(keys)


def _reply_message(
    session_factory: sessionmaker[Session],
    *,
    source_id: UUID,
    parent_message_id: int,
):
    with session_factory() as session:
        return session.execute(
            text(
                """
                SELECT m.telegram_message_id,
                       COALESCE(MAX(d.revision_index), 0) AS revision_index,
                       MAX(d.decision) FILTER (WHERE d.revision_index = 0) AS decision0,
                       MAX(d.action) FILTER (WHERE d.revision_index = 0) AS action0
                FROM messages AS m
                JOIN ai_message_decisions AS d ON d.message_id = m.id
                WHERE m.source_id = :source_id
                  AND lower(trim(m.raw_text)) = 'close all'
                  AND (m.raw_payload ->> 'reply_to_message_id')::bigint = :parent_message_id
                GROUP BY m.id, m.telegram_message_id
                ORDER BY m.telegram_message_id DESC
                LIMIT 1
                """
            ),
            {
                "source_id": source_id,
                "parent_message_id": parent_message_id,
            },
        ).mappings().first()


async def run_day28_acceptance_recovery(
    *,
    session_factory: sessionmaker[Session],
) -> None:
    if os.getenv("SUPER_SIGNALS_DAY28_ACCEPTANCE_RECOVERY", "").strip() != "1":
        return

    try:
        owner_user_id = UUID(os.environ["SUPER_SIGNALS_DAY28_OWNER_ID"].strip())
        source_values = [
            UUID(value.strip())
            for value in os.environ["SUPER_SIGNALS_DAY28_SOURCE_IDS"].split(",")
            if value.strip()
        ]
        parent_message_id = int(
            os.environ["SUPER_SIGNALS_DAY28_RECOVERY_PARENT_MESSAGE_ID"].strip()
        )
        if len(source_values) != 1:
            raise ValueError("one source required")
        source_id = source_values[0]
    except (KeyError, ValueError):
        logger.error("Day 28 acceptance recovery skipped: invalid configuration")
        return

    try:
        # Wait for the normal listener's bounded catch-up to persist and supervise the
        # real Telegram reply. Catch-up itself intentionally does not touch the broker.
        reply = None
        for _ in range(30):
            await asyncio.sleep(1)
            reply = _reply_message(
                session_factory,
                source_id=source_id,
                parent_message_id=parent_message_id,
            )
            if reply is not None:
                break
        if reply is None:
            raise RuntimeError("day28_recovery_close_reply_not_found")

        telegram_message_id = int(reply["telegram_message_id"])
        revision_index = int(reply["revision_index"] or 0)
        broker_cipher = _cipher()
        read = MetaApiReadGateway()
        trade = MetaApiTradeGateway()
        router = Day28FullExecutionRouter(
            session_factory=session_factory,
            owner_user_id=owner_user_id,
            execution_service=AtomicDay26Mt5ExecutionService(
                session_factory=session_factory,
                cipher=broker_cipher,
                read_gateway=read,
                margin_gateway=MetaApiMarginGateway(),
                trade_gateway=trade,
            ),
            management_service=Day27Mt5ManagementService(
                session_factory=session_factory,
                cipher=broker_cipher,
                read_gateway=read,
                trade_gateway=trade,
            ),
            allowed_source_ids=(source_id,),
            risk_percent="1",
            double_lot_approved=True,
        )
        result = await router.dispatch_stored_decision(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )
        # Day 27 returns the counters from the original successful event when it
        # detects an already-applied replay. For this invocation, the broker action
        # count is mechanically zero because _existing_success returns before any
        # broker state read or mutation. Make that distinction explicit in evidence.
        replay_broker_actions = 0 if result.already_applied else result.broker_actions_sent
        logger.warning(
            "Day 28 replay acceptance outcome=%s already_applied=%s signal=%s close_message_id=%d replay_broker_actions=%d",
            result.outcome,
            result.already_applied,
            result.signal_id,
            telegram_message_id,
            replay_broker_actions,
        )
    except Exception:
        logger.exception("Day 28 acceptance recovery failed")
