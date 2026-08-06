"""Database-backed owner authentication operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.security import hash_token, new_token, privacy_hash, verify_password

_DUMMY_PASSWORD_HASH = (
    "scrypt$n=32768$r=8$p=1$c3VwZXItc2lnbmFscy1kbQ$puUPWva99xMDxi6WGb_sern6ymlGtbRxUc3Xj5Hb_aQ"
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def authenticate_owner(session: Session, email: str, password: str) -> dict[str, Any] | None:
    owner = (
        session.execute(
            text(
                """
                SELECT u.id, u.email, u.display_name, u.password_hash,
                       u.two_factor_enabled, u.passkey_enabled
                FROM users AS u
                JOIN user_roles AS ur ON ur.user_id = u.id
                JOIN roles AS r ON r.id = ur.role_id
                WHERE lower(u.email::text) = lower(:email)
                  AND u.status = 'active'
                  AND r.name = 'owner'
                LIMIT 1
                """
            ),
            {"email": email.strip()},
        )
        .mappings()
        .first()
    )
    encoded = owner["password_hash"] if owner else _DUMMY_PASSWORD_HASH
    if not verify_password(password, encoded):
        return None
    return dict(owner)


def create_owner_session(
    session: Session,
    *,
    user_id: UUID,
    ttl_seconds: int,
    user_agent: str | None,
    ip_address: str | None,
    fingerprint_secret: str,
) -> tuple[str, datetime]:
    raw_token = new_token()
    expires_at = utc_now() + timedelta(seconds=ttl_seconds)
    session.execute(
        text(
            """
            INSERT INTO auth_sessions (
                user_id, token_hash, user_agent_hash, ip_hash, expires_at
            )
            VALUES (
                :user_id, :token_hash, :user_agent_hash, :ip_hash, :expires_at
            )
            """
        ),
        {
            "user_id": user_id,
            "token_hash": hash_token(raw_token),
            "user_agent_hash": privacy_hash(user_agent, fingerprint_secret),
            "ip_hash": privacy_hash(ip_address, fingerprint_secret),
            "expires_at": expires_at,
        },
    )
    session.commit()
    return raw_token, expires_at


def get_owner_for_session(session: Session, raw_token: str) -> dict[str, Any] | None:
    owner = (
        session.execute(
            text(
                """
                SELECT u.id, u.email, u.display_name,
                       u.two_factor_enabled, u.passkey_enabled,
                       s.id AS session_id, s.expires_at
                FROM auth_sessions AS s
                JOIN users AS u ON u.id = s.user_id
                JOIN user_roles AS ur ON ur.user_id = u.id
                JOIN roles AS r ON r.id = ur.role_id
                WHERE s.token_hash = :token_hash
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                  AND u.status = 'active'
                  AND r.name = 'owner'
                LIMIT 1
                """
            ),
            {"token_hash": hash_token(raw_token)},
        )
        .mappings()
        .first()
    )
    if owner is None:
        return None

    session.execute(
        text("UPDATE auth_sessions SET last_seen_at = now() WHERE id = :session_id"),
        {"session_id": owner["session_id"]},
    )
    session.commit()
    return dict(owner)


def revoke_session(session: Session, raw_token: str) -> None:
    session.execute(
        text(
            """
            UPDATE auth_sessions
            SET revoked_at = COALESCE(revoked_at, now())
            WHERE token_hash = :token_hash
            """
        ),
        {"token_hash": hash_token(raw_token)},
    )
    session.commit()


def create_recovery_request(
    session: Session,
    *,
    email: str,
    ttl_seconds: int,
    ip_address: str | None,
    fingerprint_secret: str,
) -> None:
    owner_id = session.scalar(
        text(
            """
            SELECT u.id
            FROM users AS u
            JOIN user_roles AS ur ON ur.user_id = u.id
            JOIN roles AS r ON r.id = ur.role_id
            WHERE lower(u.email::text) = lower(:email)
              AND u.status = 'active'
              AND r.name = 'owner'
            LIMIT 1
            """
        ),
        {"email": email.strip()},
    )
    if owner_id is None:
        return

    session.execute(
        text(
            """
            DELETE FROM password_recovery_requests
            WHERE user_id = :user_id
              AND used_at IS NULL
              AND expires_at <= now()
            """
        ),
        {"user_id": owner_id},
    )
    raw_token = new_token()
    session.execute(
        text(
            """
            INSERT INTO password_recovery_requests (
                user_id, token_hash, requested_ip_hash, expires_at
            )
            VALUES (:user_id, :token_hash, :requested_ip_hash, :expires_at)
            """
        ),
        {
            "user_id": owner_id,
            "token_hash": hash_token(raw_token),
            "requested_ip_hash": privacy_hash(ip_address, fingerprint_secret),
            "expires_at": utc_now() + timedelta(seconds=ttl_seconds),
        },
    )
    session.commit()
