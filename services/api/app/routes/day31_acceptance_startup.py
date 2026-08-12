"""Temporary one-shot Day 31 acceptance startup hook.

Removed before Day 31 is merged. It uses real Render Postgres/encryption with
fake broker read/trade gateways, so no real Vantage position is created or closed.
"""

from __future__ import annotations

import os

from app.day31_live_acceptance import run_day31_live_acceptance
from app.db import get_session_factory
from app.metaapi_gateway import MetaApiProvisioningGateway
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_crypto import MetaApiTokenCipher
from app.routes.user_mt5_accounts import router


@router.on_event("startup")
async def _run_day31_acceptance_once() -> None:
    if os.getenv("SUPER_SIGNALS_DAY31_ACCEPTANCE", "").strip() != "1":
        return
    key_value = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    keys = tuple(value.strip() for value in key_value.split(",") if value.strip())
    if not keys:
        raise RuntimeError("Day 31 acceptance requires broker credential encryption keys")
    service = Day30Mt5ConnectionService(
        session_factory=get_session_factory(),
        cipher=MetaApiTokenCipher(keys),
        gateway=MetaApiProvisioningGateway(),
    )
    await run_day31_live_acceptance(service)
