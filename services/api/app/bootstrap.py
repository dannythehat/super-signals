"""Idempotent hosted-environment bootstrap for the Smart Signals owner account."""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_engine
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


if __name__ == "__main__":
    main()
