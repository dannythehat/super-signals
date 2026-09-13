"""One-shot owner-authorized partner onboarding for a live Super Signals member.

The workflow is disabled unless SMART_SIGNALS_PARTNER_ONBOARDING_ENABLED=1.
Credentials are read only from runtime environment variables and are never logged or
persisted in plaintext. The MT5 password is passed to the existing connection service,
which stores only encrypted MetaAPI connection material.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets

from sqlalchemy import text

from app.db import get_session_factory
from app.metaapi_gateway import MetaApiProvisioningGateway
from app.models import AuditEvent, User, UserRole
from app.mt5_account_profiles import Mt5AccountProfileService
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_crypto import MetaApiTokenCipher
from app.security import hash_password

logger = logging.getLogger(__name__)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing_{name.lower()}")
    return value


def _ensure_member() -> tuple[object, object]:
    email = _required("SMART_SIGNALS_PARTNER_EMAIL").lower()
    display_name = os.getenv("SMART_SIGNALS_PARTNER_DISPLAY_NAME", "Partner").strip() or "Partner"
    session_factory = get_session_factory()

    with session_factory() as session:
        owner_id = session.execute(
            text(
                """
                SELECT u.id
                FROM users u
                JOIN user_roles ur ON ur.user_id=u.id
                JOIN roles r ON r.id=ur.role_id
                WHERE u.status='active' AND r.name='owner'
                ORDER BY u.created_at ASC
                LIMIT 1
                """
            )
        ).scalar_one_or_none()
        if owner_id is None:
            raise RuntimeError("partner_onboarding_owner_missing")

        role_id = session.execute(
            text("SELECT id FROM roles WHERE name='user' LIMIT 1")
        ).scalar_one_or_none()
        if role_id is None:
            raise RuntimeError("partner_onboarding_user_role_missing")

        user = session.execute(
            text(
                "SELECT id, status FROM users WHERE lower(email::text)=lower(:email) LIMIT 1"
            ),
            {"email": email},
        ).mappings().first()

        created = False
        if user is None:
            account = User(email=email, display_name=display_name[:120], status="active")
            session.add(account)
            session.flush()
            # Give the member a non-recoverable random bootstrap password. No plaintext
            # value is emitted or retained by this workflow; normal member password
            # setup/recovery remains a separate user-facing concern.
            session.execute(
                text("UPDATE users SET password_hash=:password_hash WHERE id=:user_id"),
                {
                    "password_hash": hash_password(secrets.token_urlsafe(48)),
                    "user_id": account.id,
                },
            )
            session.add(
                UserRole(
                    user_id=account.id,
                    role_id=role_id,
                    granted_by_user_id=owner_id,
                )
            )
            user_id = account.id
            created = True
        else:
            user_id = user["id"]
            session.execute(
                text(
                    "UPDATE users SET display_name=:display_name, status='active', updated_at=now() WHERE id=:user_id"
                ),
                {"display_name": display_name[:120], "user_id": user_id},
            )
            session.execute(
                text(
                    """
                    INSERT INTO user_roles (user_id, role_id, granted_by_user_id)
                    VALUES (:user_id, :role_id, :owner_id)
                    ON CONFLICT (user_id, role_id) DO NOTHING
                    """
                ),
                {"user_id": user_id, "role_id": role_id, "owner_id": owner_id},
            )

        session.execute(
            text(
                """
                INSERT INTO complimentary_access_grants
                    (user_id, status, granted_by_user_id, source, granted_at, revoked_at, updated_at)
                VALUES
                    (:user_id, 'active', :owner_id, 'owner_partner_onboarding', now(), NULL, now())
                ON CONFLICT (user_id) DO UPDATE SET
                    status='active',
                    granted_by_user_id=EXCLUDED.granted_by_user_id,
                    source=EXCLUDED.source,
                    granted_at=EXCLUDED.granted_at,
                    revoked_at=NULL,
                    updated_at=EXCLUDED.updated_at
                """
            ),
            {"user_id": user_id, "owner_id": owner_id},
        )
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type="access.partner_member_onboarded",
                entity_type="user",
                entity_id=user_id,
                payload={
                    "email": email,
                    "display_name": display_name[:120],
                    "created": created,
                    "complimentary_access": True,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()
        return user_id, owner_id


async def _connect_mt5(user_id: object, owner_id: object) -> None:
    login = _required("SMART_SIGNALS_PARTNER_MT5_LOGIN")
    password = _required("SMART_SIGNALS_PARTNER_MT5_PASSWORD")
    server = _required("SMART_SIGNALS_PARTNER_MT5_SERVER")

    broker_key_value = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    broker_keys = tuple(value.strip() for value in broker_key_value.split(",") if value.strip())
    if not broker_keys:
        raise RuntimeError("partner_onboarding_broker_keys_missing")

    session_factory = get_session_factory()
    service = Day30Mt5ConnectionService(
        session_factory=session_factory,
        cipher=MetaApiTokenCipher(broker_keys),
        gateway=MetaApiProvisioningGateway(),
    )
    service.approve_user_account(
        approver_user_id=owner_id,
        user_id=user_id,
        login=login,
        server=server,
    )
    view = await service.connect_user_live(
        user_id=user_id,
        login=login,
        password=password,
        server=server,
    )
    profiles = Mt5AccountProfileService(
        session_factory=session_factory,
        connection_service=service,
    )
    profiles.sync_active_profile_from_canonical(user_id)

    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type="mt5.partner_onboarding_completed",
                entity_type="user",
                entity_id=user_id,
                payload={
                    "account_environment": "live",
                    "broker": "vantage",
                    "platform": "mt5",
                    "login_last4": login[-4:],
                    "server": server,
                    "status": view.status,
                    "remote_state": view.remote_state,
                    "remote_connection_status": view.remote_connection_status,
                    "password_stored": False,
                },
            )
        )
        session.commit()

    logger.info(
        "Partner onboarding MT5 completed status=%s remote_state=%s remote_connection_status=%s",
        view.status,
        view.remote_state,
        view.remote_connection_status,
    )


async def main() -> None:
    if os.getenv("SMART_SIGNALS_PARTNER_ONBOARDING_ENABLED", "").strip() != "1":
        return
    try:
        user_id, owner_id = _ensure_member()
        await _connect_mt5(user_id, owner_id)
    except Mt5ConnectionError as exc:
        logger.error("Partner onboarding MT5 failed code=%s", exc.code)
    except Exception:
        logger.exception("Partner onboarding failed safely")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
