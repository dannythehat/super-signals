"""Start the API with a safe owner reconnect override for stale live MT5 mappings.

A disconnected MetaAPI terminal can become stale or disappear remotely while the
local Smart Signals row still points at its old account id. The canonical Day 30
connection service already handles this correctly: it only reuses a genuinely
CONNECTED matching terminal and otherwise provisions a fresh terminal from the
credentials supplied on the current owner request, then replaces the local mapping.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import uvicorn
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import AuditEvent
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
import app.routes.member_onboarding_owner as member_onboarding_owner


async def _refresh_credentials_and_redeploy(
    *,
    service: Day30Mt5ConnectionService,
    session: Session,
    owner_id: UUID,
    user_id: UUID,
    password: str,
    server: str,
) -> Any:
    row = session.execute(
        text(
            "SELECT login, server FROM mt5_accounts "
            "WHERE owner_user_id=:user_id LIMIT 1"
        ),
        {"user_id": user_id},
    ).mappings().first()
    if row is None:
        raise Mt5ConnectionError("mt5_account_not_configured")

    login = str(row["login"])
    canonical_server = str(row["server"] or server).strip()

    # Use the canonical live connection flow. Its gateway deliberately ignores
    # DEPLOYED/DISCONNECTED stale terminals and provisions a fresh MetaAPI terminal
    # from the password entered on this request. _store_user_connection then replaces
    # the stale local metaapi_account_id instead of creating another Smart Signals user.
    view = await service.connect_user_live(
        user_id=user_id,
        login=login,
        password=password,
        server=canonical_server,
    )

    connected = (
        str(view.status).lower() == "connected"
        and str(view.remote_connection_status or "").lower() == "connected"
    )
    session.add(
        AuditEvent(
            actor_user_id=owner_id,
            event_type=(
                "mt5.owner_onboarding_fresh_terminal_connected"
                if connected
                else "mt5.owner_onboarding_fresh_terminal_failed"
            ),
            entity_type="user",
            entity_id=user_id,
            payload={
                "connection_method": "canonical_fresh_terminal",
                "remote_state": view.remote_state,
                "remote_connection_status": view.remote_connection_status,
                "last_error_code": view.last_error_code,
                "trade_action_created": False,
            },
        )
    )
    session.commit()

    if not connected:
        raise Mt5ConnectionError(
            str(view.last_error_code or "metaapi_broker_not_connected")
        )
    return view


member_onboarding_owner._refresh_credentials_and_redeploy = _refresh_credentials_and_redeploy


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
