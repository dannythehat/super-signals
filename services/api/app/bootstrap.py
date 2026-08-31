"""Idempotent hosted-environment bootstrap for owner and temporary member setup."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_engine, get_session_factory
from app.metaapi_gateway import MetaApiProvisioningGateway
from app.mt5_account_profiles import Mt5AccountProfileService
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_crypto import MetaApiTokenCipher
from app.security import hash_password, verify_password
from app.seed import seed_owner


def ensure_owner_credentials(
    session: Session,
    email: str,
    password: str,
    display_name: str,
) -> bool:
    """Create the owner and set or rotate its password only when necessary."""

    account = seed_owner(session, email, display_name)
    current_hash = session.scalar(
        text("SELECT password_hash FROM users WHERE id = :user_id"),
        {"user_id": account.id},
    )
    if verify_password(password, current_hash):
        return False

    session.execute(
        text(
            """
            UPDATE users
            SET password_hash = :password_hash,
                updated_at = now()
            WHERE id = :user_id
            """
        ),
        {"password_hash": hash_password(password), "user_id": account.id},
    )
    if current_hash:
        session.execute(
            text(
                """
                UPDATE auth_sessions
                SET revoked_at = COALESCE(revoked_at, now())
                WHERE user_id = :user_id
                """
            ),
            {"user_id": account.id},
        )
    session.commit()
    return True


def grant_configured_complimentary_access(
    session: Session,
    *,
    owner_email: str,
    member_emails: tuple[str, ...],
) -> int:
    """Grant one-time complimentary access to configured active member accounts."""

    if not member_emails:
        return 0

    owner_id = session.execute(
        text("SELECT id FROM users WHERE lower(email::text)=lower(:email) LIMIT 1"),
        {"email": owner_email},
    ).scalar_one_or_none()
    if owner_id is None:
        print("Complimentary bootstrap skipped: owner account not found")
        return 0

    granted = 0
    for email in member_emails:
        member = session.execute(
            text(
                """
                SELECT id, status
                FROM users
                WHERE lower(email::text)=lower(:email)
                LIMIT 1
                """
            ),
            {"email": email},
        ).mappings().first()
        if member is None:
            print(f"Complimentary bootstrap skipped missing member: {email}")
            continue
        if member["status"] != "active":
            print(f"Complimentary bootstrap skipped inactive member: {email}")
            continue

        session.execute(
            text(
                """
                INSERT INTO complimentary_access_grants
                    (user_id, status, granted_by_user_id, source, granted_at, revoked_at, updated_at)
                VALUES
                    (:user_id, 'active', :owner_id, 'owner_bootstrap_env', now(), NULL, now())
                ON CONFLICT (user_id) DO UPDATE SET
                    status='active',
                    granted_by_user_id=EXCLUDED.granted_by_user_id,
                    source=EXCLUDED.source,
                    granted_at=EXCLUDED.granted_at,
                    revoked_at=NULL,
                    updated_at=EXCLUDED.updated_at
                """
            ),
            {"user_id": member["id"], "owner_id": owner_id},
        )
        session.execute(
            text(
                """
                UPDATE complimentary_access_tokens
                SET used_at=COALESCE(used_at, now())
                WHERE user_id=:user_id
                """
            ),
            {"user_id": member["id"]},
        )
        granted += 1
        print(f"Complimentary access active: {email}")

    if granted:
        session.commit()
    return granted


async def run_member_mt5_bootstrap() -> None:
    """One-shot member MT5 connection using temporary environment credentials.

    Broker passwords are read only from process environment, are never logged or
    persisted by Smart Signals, and should be cleared from Render immediately after
    the acceptance run.
    """

    email = os.getenv("SUPER_SIGNALS_MEMBER_MT5_BOOTSTRAP_EMAIL", "").strip().lower()
    login = os.getenv("SUPER_SIGNALS_MEMBER_MT5_BOOTSTRAP_LOGIN", "").strip()
    password = os.getenv("SUPER_SIGNALS_MEMBER_MT5_BOOTSTRAP_PASSWORD", "")
    server = os.getenv("SUPER_SIGNALS_MEMBER_MT5_BOOTSTRAP_SERVER", "").strip()
    if not any((email, login, password, server)):
        return
    if not all((email, login, password, server)):
        print("Member MT5 bootstrap skipped: temporary configuration is incomplete")
        return

    session_factory = get_session_factory()
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT id, status
                FROM users
                WHERE lower(email::text)=lower(:email)
                LIMIT 1
                """
            ),
            {"email": email},
        ).mappings().first()
    if row is None or str(row["status"]) != "active":
        print(f"Member MT5 bootstrap skipped: active member not found for {email}")
        return

    key_value = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    keys = tuple(value.strip() for value in key_value.split(",") if value.strip())
    if not keys:
        print("Member MT5 bootstrap skipped: broker encryption keys are unavailable")
        return

    user_id = UUID(str(row["id"]))
    service = Day30Mt5ConnectionService(
        session_factory=session_factory,
        cipher=MetaApiTokenCipher(keys),
        gateway=MetaApiProvisioningGateway(),
    )
    try:
        token = service.resolve_platform_token()
        view = await service.connect_owner_demo(
            owner_user_id=user_id,
            metaapi_token=token,
            login=login,
            password=password,
            server=server,
        )
        if view.status == "connected":
            Mt5AccountProfileService(
                session_factory=session_factory,
                connection_service=service,
            ).sync_active_profile_from_canonical(user_id)
        print(
            "Member MT5 bootstrap completed "
            f"email={email} status={view.status} "
            f"remote_state={view.remote_state} "
            f"remote_connection_status={view.remote_connection_status}"
        )
    except Mt5ConnectionError as exc:
        print(f"Member MT5 bootstrap failed email={email} code={exc.code}")
    except Exception as exc:  # noqa: BLE001 - never expose exception text/secrets
        print(f"Member MT5 bootstrap failed email={email} code={type(exc).__name__}")


def main() -> None:
    email = os.getenv("SUPER_SIGNALS_OWNER_EMAIL", "").strip()
    password = os.getenv("SUPER_SIGNALS_OWNER_PASSWORD", "")
    display_name = os.getenv("SUPER_SIGNALS_OWNER_DISPLAY_NAME", "Owner").strip() or "Owner"
    complimentary_emails = tuple(
        value.strip().lower()
        for value in os.getenv("SUPER_SIGNALS_COMPLIMENTARY_EMAILS", "").split(",")
        if value.strip()
    )

    if not email:
        raise RuntimeError("SUPER_SIGNALS_OWNER_EMAIL must be configured")
    if not password:
        raise RuntimeError("SUPER_SIGNALS_OWNER_PASSWORD must be configured")

    with Session(get_engine()) as session:
        changed = ensure_owner_credentials(session, email, password, display_name)
        granted = grant_configured_complimentary_access(
            session,
            owner_email=email,
            member_emails=complimentary_emails,
        )

    status = "created or rotated" if changed else "already configured"
    print(f"Owner account {status}: {email}")
    if complimentary_emails:
        print(f"Complimentary bootstrap grants active: {granted}")

    asyncio.run(run_member_mt5_bootstrap())


if __name__ == "__main__":
    main()
