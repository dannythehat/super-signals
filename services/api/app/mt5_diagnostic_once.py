"""Read-only one-time diagnostics for a member MT5 terminal.

No broker password is read, no account settings are changed, and no trade action is
created. The job records only sanitized MetaAPI connection metadata needed to explain
why a provisioned terminal remains disconnected.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text

from app.db import get_session_factory
from app.metaapi_gateway import MetaApiProvisioningGateway
from app.models import AuditEvent
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_crypto import MetaApiTokenCipher

logger = logging.getLogger(__name__)


def _keys() -> tuple[str, ...]:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    return tuple(part.strip() for part in raw.split(",") if part.strip())


async def main() -> None:
    email = os.getenv("SMART_SIGNALS_MT5_DIAGNOSTIC_EMAIL", "").strip().lower()
    if not email:
        return

    session_factory = get_session_factory()
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT u.id AS user_id, a.metaapi_account_id, a.login, a.server
                FROM users u
                JOIN mt5_accounts a ON a.owner_user_id=u.id
                WHERE lower(u.email::text)=lower(:email)
                LIMIT 1
                """
            ),
            {"email": email},
        ).mappings().first()
        if row is None:
            logger.error("MT5 diagnostic skipped code=member_account_missing")
            return
        user_id = row["user_id"]
        remote_account_id = str(row["metaapi_account_id"])
        login = str(row["login"])
        server = str(row["server"])

    keys = _keys()
    if not keys:
        logger.error("MT5 diagnostic skipped code=broker_keys_missing")
        return

    gateway = MetaApiProvisioningGateway(timeout_seconds=45.0)
    service = Day30Mt5ConnectionService(
        session_factory=session_factory,
        cipher=MetaApiTokenCipher(keys),
        gateway=gateway,
    )

    try:
        token = service.resolve_platform_token()
        response = await gateway._request(  # noqa: SLF001 - read-only diagnostics
            "GET",
            f"/users/current/accounts/{remote_account_id}",
            token=token,
        )
        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            payload = {}

        connections = payload.get("connections")
        replicas = payload.get("accountReplicas")
        safe_payload = {
            "login_last4": login[-4:],
            "server": server,
            "remote_state": str(payload.get("state") or "UNKNOWN").upper(),
            "remote_connection_status": str(payload.get("connectionStatus") or "UNKNOWN").upper(),
            "type": str(payload.get("type") or ""),
            "version": payload.get("version"),
            "region": str(payload.get("region") or ""),
            "primary_replica": payload.get("primaryReplica"),
            "connections_count": len(connections) if isinstance(connections, list) else None,
            "replicas_count": len(replicas) if isinstance(replicas, list) else None,
            "resource_slots": payload.get("resourceSlots"),
            "reliability": str(payload.get("reliability") or ""),
            "trade_action_created": False,
        }

        with session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="mt5.safe_diagnostic_snapshot",
                    entity_type="user",
                    entity_id=user_id,
                    payload=safe_payload,
                )
            )
            session.commit()
        logger.info(
            "MT5 diagnostic state=%s connection=%s type=%s version=%s region=%s connections=%s",
            safe_payload["remote_state"],
            safe_payload["remote_connection_status"],
            safe_payload["type"],
            safe_payload["version"],
            safe_payload["region"],
            safe_payload["connections_count"],
        )
    except Exception as exc:
        logger.error("MT5 diagnostic failed kind=%s", type(exc).__name__)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
