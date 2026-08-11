"""One-shot, opt-in Day 23 live-state acceptance probe.

This is disabled by default and performs no trades. When explicitly enabled for
one deployment it reads the owner's live terminal state once, allowing the normal
Day 23 audit event to retain safe acceptance evidence in PostgreSQL.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError

logger = logging.getLogger(__name__)


async def run_day23_acceptance_probe(
    *,
    session_factory: sessionmaker[Session],
    cipher: MetaApiTokenCipher,
) -> None:
    if os.getenv("SUPER_SIGNALS_DAY23_ACCEPTANCE_PROBE", "").strip() != "1":
        return

    owner_id_raw = os.getenv("SUPER_SIGNALS_DAY23_OWNER_ID", "").strip()
    try:
        owner_user_id = UUID(owner_id_raw)
    except ValueError:
        logger.error("Day 23 acceptance probe skipped: owner id is invalid")
        return

    service = Day23Mt5ReadService(
        session_factory=session_factory,
        cipher=cipher,
        gateway=MetaApiReadGateway(),
    )
    try:
        state = await service.read_owner_live_state(owner_user_id)
    except Day23ReadError as exc:
        logger.error(
            "Day 23 acceptance probe failed code=%s retryable=%s",
            exc.code,
            exc.retryable,
        )
        return

    logger.info(
        "Day 23 acceptance probe completed symbol=%s region=%s positions=%d price_available=%s price_stale=%s execution_ready=%s trade_action_created=false",
        state.price.symbol,
        state.region,
        len(state.positions),
        state.price.available,
        state.price.stale,
        state.execution_ready,
    )
