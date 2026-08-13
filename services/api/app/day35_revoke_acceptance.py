"""One-shot Day 35 live revoke acceptance for the Owner self-test member.

This module is intentionally hard-bound to the dedicated acceptance email and only
runs when the explicit Render flag is enabled. It uses the production Day 35 revoke
service rather than mutating database state directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from uuid import UUID

from sqlalchemy import text

from app.admin_user_controls_day35 import Day35AdminUserControlService
from app.db import get_session_factory
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.trading_controls_day31 import Day31TradingControlService

logger = logging.getLogger(__name__)
TARGET_EMAIL = "dannythetruther@gmail.com"


async def _run() -> None:
    if os.getenv("SUPER_SIGNALS_DAY35_REVOKE_ACCEPTANCE", "").strip() != "1":
        return

    owner_email = os.getenv("SUPER_SIGNALS_OWNER_EMAIL", "").strip().lower()
    if not owner_email:
        raise RuntimeError("Day 35 revoke acceptance requires configured Owner email")

    session_factory = get_session_factory()
    with session_factory() as session:
        target = session.execute(
            text("SELECT id::text,status FROM users WHERE lower(email::text)=:email"),
            {"email": TARGET_EMAIL},
        ).mappings().one_or_none()
        owner = session.execute(
            text("SELECT id::text,status FROM users WHERE lower(email::text)=:email"),
            {"email": owner_email},
        ).mappings().one_or_none()

    if target is None or owner is None:
        raise RuntimeError("Day 35 revoke acceptance identities are missing")
    if owner["status"] != "active":
        raise RuntimeError("Day 35 revoke acceptance Owner is not active")
    if target["status"] == "revoked":
        logger.info("Day 35 revoke acceptance already completed target=%s", TARGET_EMAIL)
        return
    if target["status"] != "active":
        raise RuntimeError("Day 35 revoke acceptance target is not active")

    key_value = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    keys = tuple(value.strip() for value in key_value.split(",") if value.strip())
    if not keys:
        raise RuntimeError("Day 35 revoke acceptance broker encryption key is unavailable")

    trading = Day31TradingControlService(
        session_factory=session_factory,
        cipher=MetaApiTokenCipher(keys),
        read_gateway=MetaApiReadGateway(),
        trade_gateway=MetaApiTradeGateway(),
    )
    service = Day35AdminUserControlService(
        session_factory=session_factory,
        trading_service=trading,
    )
    target_id = UUID(target["id"])
    result = await service.revoke_user(
        user_id=target_id,
        actor_user_id=UUID(owner["id"]),
        confirmed=True,
        confirmation_text=f"REVOKE {TARGET_EMAIL}",
    )
    logger.warning(
        "Day 35 revoke acceptance completed target=%s status=%s automation=%s sessions=%s approvals=%s push=%s broker_actions=%s mapped_closed=%s manual_touched=%s",
        TARGET_EMAIL,
        result.status,
        result.automation_status,
        result.sessions_revoked,
        result.approvals_revoked,
        result.push_devices_disabled,
        result.broker_actions_sent,
        result.mapped_positions_closed,
        result.manual_or_unmapped_positions_touched,
    )


if __name__ == "__main__":
    asyncio.run(_run())
