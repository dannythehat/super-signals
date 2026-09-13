"""Owner-authorized one-time recovery for a stuck live member MT5 terminal.

The job is disabled unless SMART_SIGNALS_ONE_TIME_MT5_REDEPLOY_EMAIL is set.
It never receives or stores the member MT5 password. It asks MetaAPI to redeploy the
already-provisioned remote terminal, then mirrors the broker connection state back to
Smart Signals. Trading remains stopped unless MetaAPI reports CONNECTED.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from sqlalchemy import text

from app.db import get_session_factory
from app.metaapi_gateway import MetaApiGatewayError, MetaApiProvisioningGateway
from app.models import AuditEvent
from app.mt5_account_profiles import Mt5AccountProfileService
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_crypto import MetaApiTokenCipher

logger = logging.getLogger(__name__)


def _keys() -> tuple[str, ...]:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    return tuple(value.strip() for value in raw.split(",") if value.strip())


async def main() -> None:
    email = os.getenv("SMART_SIGNALS_ONE_TIME_MT5_REDEPLOY_EMAIL", "").strip().lower()
    if not email:
        return
    run_id = os.getenv("SMART_SIGNALS_ONE_TIME_MT5_REDEPLOY_RUN_ID", "default").strip() or "default"

    session_factory = get_session_factory()
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT u.id AS user_id,
                       a.id AS local_account_id,
                       a.metaapi_account_id,
                       a.account_environment,
                       a.status,
                       a.login,
                       a.server
                FROM users u
                JOIN mt5_accounts a ON a.owner_user_id=u.id
                WHERE lower(u.email::text)=lower(:email)
                LIMIT 1
                """
            ),
            {"email": email},
        ).mappings().first()
        if row is None:
            logger.error("One-time MT5 redeploy skipped code=member_mt5_missing")
            return
        if str(row["account_environment"]).lower() != "live":
            logger.error("One-time MT5 redeploy skipped code=member_mt5_not_live")
            return
        exposure = int(
            session.execute(
                text("SELECT COUNT(*) FROM positions WHERE user_id=:user_id AND status IN ('open','pending')"),
                {"user_id": row["user_id"]},
            ).scalar_one()
        )
        if exposure:
            logger.error("One-time MT5 redeploy skipped code=member_has_open_exposure")
            return
        already = bool(
            session.execute(
                text(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM audit_events
                        WHERE entity_id=:user_id
                          AND event_type IN ('mt5.owner_redeploy_once_completed','mt5.owner_redeploy_once_failed')
                          AND payload->>'run_id'=:run_id
                    )
                    """
                ),
                {"user_id": row["user_id"], "run_id": run_id},
            ).scalar_one()
        )
        if already:
            logger.info("One-time MT5 redeploy already processed run_id=%s", run_id)
            return
        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="mt5.owner_redeploy_once_started",
                entity_type="user",
                entity_id=row["user_id"],
                payload={"run_id": run_id, "trade_action_created": False},
            )
        )
        session.commit()
        user_id = row["user_id"]
        local_account_id = row["local_account_id"]
        remote_account_id = str(row["metaapi_account_id"])

    keys = _keys()
    if not keys:
        logger.error("One-time MT5 redeploy failed code=broker_keys_missing")
        return

    gateway = MetaApiProvisioningGateway()
    service = Day30Mt5ConnectionService(
        session_factory=session_factory,
        cipher=MetaApiTokenCipher(keys),
        gateway=gateway,
    )

    try:
        token = service.resolve_platform_token()
        remote = await gateway.read_account(token=token, account_id=remote_account_id)
        if not (remote.state == "DEPLOYED" and remote.connection_status == "CONNECTED"):
            await gateway._request(  # noqa: SLF001 - one-time controlled recovery
                "POST",
                f"/users/current/accounts/{remote_account_id}/redeploy",
                token=token,
                accepted_statuses={200, 201, 202, 204},
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                remote = await gateway.read_account(token=token, account_id=remote_account_id)
                if remote.state in {"DEPLOY_FAILED", "REDEPLOY_FAILED"}:
                    raise MetaApiGatewayError("metaapi_deploy_failed")
                if remote.state == "DEPLOYED" and remote.connection_status == "CONNECTED":
                    break
                await asyncio.sleep(2)

        service._write_remote_state(local_account_id, remote)  # noqa: SLF001
        connected = remote.state == "DEPLOYED" and remote.connection_status == "CONNECTED"

        if connected:
            Mt5AccountProfileService(
                session_factory=session_factory,
                connection_service=service,
            ).sync_active_profile_from_canonical(user_id)
            with session_factory() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO user_trading_controls
                            (user_id, risk_percent, allow_double_lot, trading_status, activated_at, stopped_at, updated_at)
                        VALUES (:user_id, 1.0, FALSE, 'active', now(), NULL, now())
                        ON CONFLICT (user_id) DO UPDATE SET
                            risk_percent=1.0,
                            allow_double_lot=FALSE,
                            trading_status='active',
                            activated_at=now(),
                            stopped_at=NULL,
                            updated_at=now()
                        """
                    ),
                    {"user_id": user_id},
                )
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_type="mt5.owner_redeploy_once_completed",
                        entity_type="user",
                        entity_id=user_id,
                        payload={
                            "run_id": run_id,
                            "status": "connected",
                            "remote_state": remote.state,
                            "remote_connection_status": remote.connection_status,
                            "risk_percent": 1.0,
                            "allow_double_lot": False,
                            "trade_action_created": False,
                        },
                    )
                )
                session.commit()
            logger.info("One-time MT5 redeploy completed status=connected")
            return

        with session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="mt5.owner_redeploy_once_failed",
                    entity_type="user",
                    entity_id=user_id,
                    payload={
                        "run_id": run_id,
                        "error_code": "metaapi_broker_not_connected",
                        "remote_state": remote.state,
                        "remote_connection_status": remote.connection_status,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
        logger.error(
            "One-time MT5 redeploy failed code=metaapi_broker_not_connected state=%s connection=%s",
            remote.state,
            remote.connection_status,
        )
    except MetaApiGatewayError as exc:
        with session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE mt5_accounts
                    SET last_error_code=:error_code,
                        last_checked_at=now(),
                        updated_at=now()
                    WHERE id=:account_id
                    """
                ),
                {"error_code": exc.code, "account_id": local_account_id},
            )
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="mt5.owner_redeploy_once_failed",
                    entity_type="user",
                    entity_id=user_id,
                    payload={
                        "run_id": run_id,
                        "error_code": exc.code,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
        logger.error("One-time MT5 redeploy failed code=%s", exc.code)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
