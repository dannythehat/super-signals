"""Database-backed authentication and session operations."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.permissions import primary_role
from app.security import hash_token, new_token, privacy_hash, verify_password

_DUMMY_PASSWORD_HASH = (
    "scrypt$n=32768$r=8$p=1$c3VwZXItc2lnbmFscy1kbQ$puUPWva99xMDxi6WGb_sern6ymlGtbRxUc3Xj5Hb_aQ"
)
# A session may slide while the device is active, but it can never survive
# indefinitely. This protects production even if a stale environment variable
# accidentally asks for a multi-year session.
_MAX_SESSION_ABSOLUTE_SECONDS = 30 * 24 * 60 * 60


def utc_now() -> datetime:
    return datetime.now(UTC)


def _load_identity(session: Session, user_id: UUID) -> dict[str, Any] | None:
    account = (
        session.execute(
            text(
                """
                SELECT id, email, display_name, two_factor_enabled, passkey_enabled
                FROM users
                WHERE id = :user_id
                  AND status = 'active'
                """
            ),
            {"user_id": user_id},
        )
        .mappings()
        .first()
    )
    if account is None:
        return None

    roles = list(
        session.scalars(
            text(
                """
                SELECT r.name
                FROM user_roles AS ur
                JOIN roles AS r ON r.id = ur.role_id
                WHERE ur.user_id = :user_id
                ORDER BY CASE r.name
                    WHEN 'owner' THEN 1
                    WHEN 'trading_admin' THEN 2
                    WHEN 'user' THEN 3
                    ELSE 99
                END
                """
            ),
            {"user_id": user_id},
        )
    )
    if not roles:
        return None

    permissions = list(
        session.scalars(
            text(
                """
                SELECT DISTINCT p.code
                FROM user_roles AS ur
                JOIN role_permissions AS rp ON rp.role_id = ur.role_id
                JOIN permissions AS p ON p.id = rp.permission_id
                WHERE ur.user_id = :user_id
                ORDER BY p.code
                """
            ),
            {"user_id": user_id},
        )
    )
    identity = dict(account)
    identity["roles"] = roles
    identity["role"] = primary_role(roles)
    identity["permissions"] = permissions
    return identity


def authenticate_user(session: Session, email: str, password: str) -> dict[str, Any] | None:
    account = (
        session.execute(
            text(
                """
                SELECT u.id, u.password_hash
                FROM users AS u
                WHERE lower(u.email::text) = lower(:email)
                  AND u.status = 'active'
                  AND u.password_hash IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM user_roles AS ur WHERE ur.user_id = u.id
                  )
                LIMIT 1
                """
            ),
            {"email": email.strip()},
        )
        .mappings()
        .first()
    )
    encoded = account["password_hash"] if account else _DUMMY_PASSWORD_HASH
    valid_password = verify_password(password, encoded)
    if account is None or not valid_password:
        return None
    return _load_identity(session, account["id"])


def create_session(
    session: Session,
    *,
    user_id: UUID,
    ttl_seconds: int,
    user_agent: str | None,
    ip_address: str | None,
    fingerprint_secret: str,
) -> tuple[str, datetime]:
    raw_token = new_token()
    effective_ttl = min(ttl_seconds, _MAX_SESSION_ABSOLUTE_SECONDS)
    expires_at = utc_now() + timedelta(seconds=effective_ttl)
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


def get_user_for_session(
    session: Session,
    raw_token: str,
    *,
    user_agent: str | None = None,
    fingerprint_secret: str | None = None,
) -> dict[str, Any] | None:
    active_session = (
        session.execute(
            text(
                """
                SELECT
                    s.id AS session_id,
                    s.user_id,
                    s.created_at,
                    s.expires_at,
                    s.user_agent_hash
                FROM auth_sessions AS s
                JOIN users AS u ON u.id = s.user_id
                WHERE s.token_hash = :token_hash
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                  AND u.status = 'active'
                LIMIT 1
                """
            ),
            {"token_hash": hash_token(raw_token)},
        )
        .mappings()
        .first()
    )
    if active_session is None:
        return None

    now = utc_now()
    hard_deadline = active_session["created_at"] + timedelta(
        seconds=_MAX_SESSION_ABSOLUTE_SECONDS
    )

    stored_user_agent_hash = active_session["user_agent_hash"]
    if fingerprint_secret is not None and stored_user_agent_hash is not None:
        current_user_agent_hash = privacy_hash(user_agent, fingerprint_secret)
        if (
            current_user_agent_hash is None
            or not hmac.compare_digest(stored_user_agent_hash, current_user_agent_hash)
        ):
            session.execute(
                text(
                    """
                    UPDATE auth_sessions
                    SET revoked_at = COALESCE(revoked_at, now())
                    WHERE id = :session_id
                    """
                ),
                {"session_id": active_session["session_id"]},
            )
            session.commit()
            return None

    if now >= hard_deadline:
        session.execute(
            text(
                """
                UPDATE auth_sessions
                SET revoked_at = COALESCE(revoked_at, now())
                WHERE id = :session_id
                """
            ),
            {"session_id": active_session["session_id"]},
        )
        session.commit()
        return None

    identity = _load_identity(session, active_session["user_id"])
    if identity is None:
        return None

    requested_renewal = now + timedelta(seconds=get_settings().session_ttl_seconds)
    renewed_expires_at = min(requested_renewal, hard_deadline)
    session.execute(
        text(
            """
            UPDATE auth_sessions
            SET last_seen_at = now(),
                expires_at = :expires_at
            WHERE id = :session_id
            """
        ),
        {
            "session_id": active_session["session_id"],
            "expires_at": renewed_expires_at,
        },
    )
    session.commit()
    identity["session_id"] = active_session["session_id"]
    identity["expires_at"] = renewed_expires_at
    return identity


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
    user_id = session.scalar(
        text(
            """
            SELECT u.id
            FROM users AS u
            WHERE lower(u.email::text) = lower(:email)
              AND u.status = 'active'
              AND EXISTS (
                  SELECT 1 FROM user_roles AS ur WHERE ur.user_id = u.id
              )
            LIMIT 1
            """
        ),
        {"email": email.strip()},
    )
    if user_id is None:
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
        {"user_id": user_id},
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
            "user_id": user_id,
            "token_hash": hash_token(raw_token),
            "requested_ip_hash": privacy_hash(ip_address, fingerprint_secret),
            "expires_at": utc_now() + timedelta(seconds=ttl_seconds),
        },
    )
    session.commit()


authenticate_owner = authenticate_user
create_owner_session = create_session
get_owner_for_session = get_user_for_session
