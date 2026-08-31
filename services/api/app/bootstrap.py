"""Idempotent hosted-environment bootstrap for the Smart Signals owner account."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_engine
from app.member_email import send_admin_new_signup
from app.models import AuditEvent, User, UserRole
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


def send_configured_onboarding_test(session: Session, *, test_email: str) -> bool:
    """Create one disposable real member signup and emit the genuine owner approval email.

    This is deliberately one-shot. A successful send is marked in the audit log so a
    process restart cannot generate another email for the same test member.
    """

    if not test_email:
        return False

    row = session.execute(
        text("SELECT id, display_name FROM users WHERE lower(email::text)=lower(:email) LIMIT 1"),
        {"email": test_email},
    ).mappings().first()

    if row is None:
        role_id = session.execute(
            text("SELECT id FROM roles WHERE name='user' LIMIT 1")
        ).scalar_one_or_none()
        if role_id is None:
            print("Onboarding test skipped: user role missing")
            return False

        user = User(
            email=test_email,
            display_name="Onboarding Test Member",
            status="active",
        )
        session.add(user)
        session.flush()
        session.execute(
            text("UPDATE users SET password_hash=:password_hash WHERE id=:user_id"),
            {
                "password_hash": hash_password(secrets.token_urlsafe(32)),
                "user_id": user.id,
            },
        )
        session.add(
            UserRole(
                user_id=user.id,
                role_id=role_id,
                granted_by_user_id=None,
            )
        )
        session.commit()
        user_id = user.id
        display_name = "Onboarding Test Member"
    else:
        user_id = row["id"]
        display_name = str(row["display_name"] or "Onboarding Test Member")

    already_sent = bool(
        session.execute(
            text(
                """
                SELECT EXISTS(
                    SELECT 1 FROM audit_events
                    WHERE actor_user_id=:user_id
                      AND event_type='access.onboarding_test_admin_email_sent'
                )
                """
            ),
            {"user_id": user_id},
        ).scalar_one()
    )
    if already_sent:
        print(f"Onboarding test already sent: {test_email}")
        return True

    complimentary_token = secrets.token_urlsafe(36)
    token_hash = hashlib.sha256(complimentary_token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    session.execute(
        text(
            """
            INSERT INTO complimentary_access_tokens (user_id, token_hash, expires_at)
            VALUES (:user_id, :token_hash, :expires_at)
            """
        ),
        {"user_id": user_id, "token_hash": token_hash, "expires_at": expires_at},
    )
    session.commit()

    delivery = send_admin_new_signup(
        member_email=test_email,
        display_name=display_name,
        complimentary_token=complimentary_token,
    )
    session.add(
        AuditEvent(
            actor_user_id=user_id,
            event_type=(
                "access.onboarding_test_admin_email_sent"
                if delivery.sent
                else "access.onboarding_test_admin_email_not_sent"
            ),
            entity_type="user",
            entity_id=user_id,
            payload={"reason": delivery.reason, "source": "one_shot_onboarding_test"},
        )
    )
    session.commit()
    if delivery.sent:
        print(f"Onboarding test admin email sent for: {test_email}")
        return True
    print(f"Onboarding test admin email failed: {delivery.reason}")
    return False


def main() -> None:
    email = os.getenv("SUPER_SIGNALS_OWNER_EMAIL", "").strip()
    password = os.getenv("SUPER_SIGNALS_OWNER_PASSWORD", "")
    display_name = os.getenv("SUPER_SIGNALS_OWNER_DISPLAY_NAME", "Owner").strip() or "Owner"
    complimentary_emails = tuple(
        value.strip().lower()
        for value in os.getenv("SUPER_SIGNALS_COMPLIMENTARY_EMAILS", "").split(",")
        if value.strip()
    )
    onboarding_test_email = os.getenv("SMART_SIGNALS_ONBOARDING_TEST_EMAIL", "").strip().lower()

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
        onboarding_test_sent = send_configured_onboarding_test(
            session,
            test_email=onboarding_test_email,
        )

    status = "created or rotated" if changed else "already configured"
    print(f"Owner account {status}: {email}")
    if complimentary_emails:
        print(f"Complimentary bootstrap grants active: {granted}")
    if onboarding_test_email:
        print(f"Onboarding test email emitted: {onboarding_test_sent}")


if __name__ == "__main__":
    main()
