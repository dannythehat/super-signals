"""One-shot exact mapped-position close requested by the Owner on 24 Sep 2026.

This hook is inert unless both environment variables are present. It reuses the same
retry-safe OwnerManualCloseService as the product UI and can only target the exact local
Smart Signals position UUID supplied by the operator.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.owner_manual_close import OwnerManualCloseError, OwnerManualCloseService

logger = logging.getLogger(__name__)

_POSITION_ENV = "SUPER_SIGNALS_ONE_SHOT_CLOSE_POSITION_ID"
_USER_ENV = "SUPER_SIGNALS_ONE_SHOT_CLOSE_USER_ID"


async def run_owner_manual_close_once(
    *,
    session_factory: sessionmaker[Session],
    cipher: MetaApiTokenCipher,
) -> None:
    position_raw = os.getenv(_POSITION_ENV, "").strip()
    user_raw = os.getenv(_USER_ENV, "").strip()
    if not position_raw or not user_raw:
        return

    try:
        position_id = UUID(position_raw)
        user_id = UUID(user_raw)
    except ValueError:
        logger.error("OWNER_ONE_SHOT_CLOSE=INVALID_TARGET")
        return

    service = OwnerManualCloseService(
        session_factory=session_factory,
        cipher=cipher,
        read_gateway=MetaApiReadGateway(timeout_seconds=5.0),
        trade_gateway=MetaApiTradeGateway(),
    )
    try:
        result = await service.close_position(
            user_id,
            position_id,
            scope="owner_api_one_shot_20260924",
        )
    except OwnerManualCloseError as exc:
        if exc.code == "owner_manual_position_not_open":
            logger.info("OWNER_ONE_SHOT_CLOSE=ALREADY_CLOSED position=%s", position_id)
            return
        logger.error(
            "OWNER_ONE_SHOT_CLOSE=FAILED position=%s code=%s retryable=%s",
            position_id,
            exc.code,
            exc.retryable,
        )
        return

    logger.info(
        "OWNER_ONE_SHOT_CLOSE=COMPLETE position=%s requested=%s closed=%s already_closed=%s failed=%s",
        position_id,
        result.requested_count,
        result.closed_count,
        result.already_closed_count,
        result.failed_count,
    )


__all__ = ["run_owner_manual_close_once"]
