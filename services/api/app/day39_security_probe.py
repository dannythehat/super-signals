"""One-shot Day 39 live security acceptance.

This probe never calls Telegram or broker gateways. It uses isolated auth rows,
cleans its temporary state and records only non-secret acceptance evidence.
"""

from __future__ import annotations

import secrets

from sqlalchemy import text

from app.auth_hardening_day39 import (
    RateLimitExceeded,
    check_rate_limit,
    record_rate_limit_failure,
)
from app.auth_service import create_session, get_user_for_session
from app.config import get_settings
from app.db import get_session_factory
from app.models import AuditEvent
from app.security import hash_token


def run_day39_security_probe() -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    probe_key = f"day39-{secrets.token_hex(16)}"
    raw_token: str | None = None
    owner_id = None

    with session_factory() as session:
        version = session.scalar(text("SELECT version_num FROM alembic_version"))
        if version != "0023_day39_security_hardening":
            raise RuntimeError(f"Day 39 migration not active: {version}")

        triggers = list(
            session.execute(
                text(
                    """
                    SELECT tgname, tgenabled
                    FROM pg_trigger
                    WHERE tgrelid = 'audit_events'::regclass
                      AND NOT tgisinternal
                    ORDER BY tgname
                    """
                )
            ).all()
        )
        if triggers != [
            ("audit_events_no_delete", "O"),
            ("audit_events_no_update", "O"),
        ]:
            raise RuntimeError("Audit append-only triggers are missing or disabled.")

        counts = dict(
            session.execute(
                text(
                    """
                    SELECT r.name, count(p.code)::int
                    FROM roles AS r
                    JOIN role_permissions AS rp ON rp.role_id = r.id
                    JOIN permissions AS p ON p.id = rp.permission_id
                    WHERE r.name IN ('owner', 'trading_admin', 'user')
                    GROUP BY r.name
                    """
                )
            ).all()
        )
        if counts != {"owner": 23, "trading_admin": 8, "user": 8}:
            raise RuntimeError(f"Unexpected permission matrix: {counts}")

        owner_id = session.scalar(
            text(
                """
                SELECT u.id
                FROM users AS u
                JOIN user_roles AS ur ON ur.user_id = u.id
                JOIN roles AS r ON r.id = ur.role_id
                WHERE u.status = 'active'
                  AND r.name = 'owner'
                ORDER BY u.created_at
                LIMIT 1
                """
            )
        )
        if owner_id is None:
            raise RuntimeError("No active Owner is available for isolated session proof.")

    # Persistent rate limiting: raw key is HMACed before storage and the bucket
    # becomes blocked after the configured test threshold.
    with session_factory() as session:
        check_rate_limit(
            session,
            scope="day39_probe",
            key_material=probe_key,
            secret=settings.session_fingerprint_secret,
        )
        for _ in range(2):
            record_rate_limit_failure(
                session,
                scope="day39_probe",
                key_material=probe_key,
                secret=settings.session_fingerprint_secret,
                limit=2,
                window_seconds=300,
                block_seconds=300,
            )
        try:
            check_rate_limit(
                session,
                scope="day39_probe",
                key_material=probe_key,
                secret=settings.session_fingerprint_secret,
            )
        except RateLimitExceeded:
            pass
        else:
            raise RuntimeError("Persistent rate-limit bucket did not block.")

        raw_key_stored = session.scalar(
            text(
                """
                SELECT count(*)
                FROM auth_rate_limits
                WHERE key_hash = :raw_value
                """
            ),
            {"raw_value": probe_key},
        )
        if raw_key_stored:
            raise RuntimeError("Raw rate-limit key material was persisted.")

        session.execute(
            text(
                """
                DELETE FROM auth_rate_limits
                WHERE scope = 'day39_probe'
                """
            )
        )
        session.commit()

    # Device/browser binding: a stolen cookie presented with a different
    # User-Agent must revoke only the isolated probe session.
    with session_factory() as session:
        raw_token, _ = create_session(
            session,
            user_id=owner_id,
            ttl_seconds=300,
            user_agent="SuperSignals-Day39-Accepted-Device",
            ip_address="127.0.0.1",
            fingerprint_secret=settings.session_fingerprint_secret,
        )

    with session_factory() as session:
        accepted = get_user_for_session(
            session,
            raw_token,
            user_agent="SuperSignals-Day39-Accepted-Device",
            fingerprint_secret=settings.session_fingerprint_secret,
        )
        if accepted is None or accepted["id"] != owner_id:
            raise RuntimeError("Same-browser session validation failed.")

    with session_factory() as session:
        stolen = get_user_for_session(
            session,
            raw_token,
            user_agent="SuperSignals-Day39-Stolen-Device",
            fingerprint_secret=settings.session_fingerprint_secret,
        )
        if stolen is not None:
            raise RuntimeError("Mismatched browser fingerprint was accepted.")
        token_hash = hash_token(raw_token)
        revoked = session.scalar(
            text(
                """
                SELECT revoked_at IS NOT NULL
                FROM auth_sessions
                WHERE token_hash = :token_hash
                """
            ),
            {"token_hash": token_hash},
        )
        if revoked is not True:
            raise RuntimeError("Mismatched browser fingerprint did not revoke the session.")
        session.execute(
            text(
                """
                DELETE FROM auth_sessions
                WHERE token_hash = :token_hash
                """
            ),
            {"token_hash": token_hash},
        )
        session.commit()

    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type="day39.security_probe_passed",
                entity_type="security",
                payload={
                    "migration": "0023_day39_security_hardening",
                    "audit_triggers_enabled": True,
                    "permission_matrix_verified": True,
                    "rate_limit_persistent": True,
                    "rate_limit_raw_key_persisted": False,
                    "session_browser_binding_verified": True,
                    "session_ip_binding_enforced": False,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()
