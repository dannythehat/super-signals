"""Start the API with the live-member MT5 reconnect fix applied in-process.

The owner reconnect endpoint previously sent an unnecessary account setting in the
MetaAPI update request. MetaAPI was rejecting that request before the broker login was
ever retried. This launcher replaces only the reconnect helper with the documented
password/name/server update payload, then starts the normal FastAPI application.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from uuid import UUID

import uvicorn
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.metaapi_gateway import MetaApiGatewayError
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
            "SELECT id, metaapi_account_id FROM mt5_accounts WHERE owner_user_id=:user_id LIMIT 1"
        ),
        {"user_id": user_id},
    ).mappings().first()
    if row is None or not row["metaapi_account_id"]:
        raise Mt5ConnectionError("mt5_account_not_configured")

    token = service.resolve_platform_token()
    gateway = service._gateway  # noqa: SLF001 - canonical service owns this gateway
    remote_account_id = str(row["metaapi_account_id"])

    try:
        # MetaAPI's documented update-account payload for credential refresh. Keep this
        # deliberately minimal so schema validation cannot block the broker retry.
        await gateway._request(  # noqa: SLF001
            "PUT",
            f"/users/current/accounts/{remote_account_id}",
            token=token,
            json={
                "password": password,
                "name": "Smart Signals MT5",
                "server": server.strip(),
            },
            accepted_statuses={200, 204},
        )
        await gateway._request(  # noqa: SLF001
            "POST",
            f"/users/current/accounts/{remote_account_id}/redeploy",
            token=token,
            accepted_statuses={200, 201, 202, 204},
        )

        deadline = time.monotonic() + 150
        remote = None
        while time.monotonic() < deadline:
            remote = await gateway.read_account(token=token, account_id=remote_account_id)
            if remote.state in {"DEPLOY_FAILED", "REDEPLOY_FAILED"}:
                raise MetaApiGatewayError("metaapi_deploy_failed")
            if remote.state == "DEPLOYED" and remote.connection_status == "CONNECTED":
                break
            await asyncio.sleep(2)

        if remote is None:
            raise MetaApiGatewayError("metaapi_timeout", retryable=True)

        service._write_remote_state(row["id"], remote)  # noqa: SLF001
        connected = remote.state == "DEPLOYED" and remote.connection_status == "CONNECTED"
        event_type = (
            "mt5.owner_onboarding_credential_refresh"
            if connected
            else "mt5.owner_onboarding_credential_refresh_failed"
        )
        payload: dict[str, object] = {
            "remote_state": remote.state,
            "remote_connection_status": remote.connection_status,
            "trade_action_created": False,
        }
        if not connected:
            payload["error_code"] = "metaapi_broker_not_connected"
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type=event_type,
                entity_type="user",
                entity_id=user_id,
                payload=payload,
            )
        )
        session.commit()
        if not connected:
            raise Mt5ConnectionError("metaapi_broker_not_connected")
    except Mt5ConnectionError:
        raise
    except MetaApiGatewayError as exc:
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type="mt5.owner_onboarding_credential_refresh_failed",
                entity_type="user",
                entity_id=user_id,
                payload={"error_code": exc.code, "trade_action_created": False},
            )
        )
        session.commit()
        raise Mt5ConnectionError(exc.code) from exc

    return service.get_user_status(user_id)


member_onboarding_owner._refresh_credentials_and_redeploy = _refresh_credentials_and_redeploy


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
