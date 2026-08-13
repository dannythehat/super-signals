"""Database-backed auth throttling for Day 39.

The key is an HMAC privacy hash of the normalized account identifier. Raw emails,
passwords, IP addresses and tokens are never persisted by this layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.security import privacy_hash


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    scope: str
    max_attempts: int
    window_seconds: int
    block_seconds: int


class RateLimitExceeded(RuntimeError):
    def __init__(self, *, scope: str) -> None:
        super().__init__(scope)
        self.scope = scope


def subject_hash(value: str, fingerprint_secret: str) -> str:
    normalized = value.strip().lower()
    hashed = privacy_hash(normalized, fingerprint_secret)
    if hashed is None:  # pragma: no cover - caller models enforce non-empty input
        raise ValueError("rate_limit_subject_required")
    return hashed


def assert_not_limited(
    session: Session,
    *,
    policy: RateLimitPolicy,
    subject: str,
    fingerprint_secret: str,
) -> None:
    key_hash = subject_hash(subject, fingerprint_secret)
    blocked = session.execute(
        text(
            """
            SELECT 1
            FROM auth_rate_limits
            WHERE scope=:scope
              AND subject_hash=:subject_hash
              AND blocked_until IS NOT NULL
              AND blocked_until > now()
            LIMIT 1
            """
        ),
        {"scope": policy.scope, "subject_hash": key_hash},
    ).scalar_one_or_none()
    if blocked:
        raise RateLimitExceeded(scope=policy.scope)


def record_failure(
    session: Session,
    *,
    policy: RateLimitPolicy,
    subject: str,
    fingerprint_secret: str,
) -> None:
    key_hash = subject_hash(subject, fingerprint_secret)
    session.execute(
        text(
            """
            INSERT INTO auth_rate_limits (
                scope, subject_hash, window_started_at, attempt_count, blocked_until, updated_at
            )
            VALUES (:scope, :subject_hash, now(), 1, NULL, now())
            ON CONFLICT (scope, subject_hash) DO UPDATE
            SET
                attempt_count = CASE
                    WHEN auth_rate_limits.window_started_at
                         <= now() - (:window_seconds * interval '1 second')
                    THEN 1
                    ELSE auth_rate_limits.attempt_count + 1
                END,
                window_started_at = CASE
                    WHEN auth_rate_limits.window_started_at
                         <= now() - (:window_seconds * interval '1 second')
                    THEN now()
                    ELSE auth_rate_limits.window_started_at
                END,
                blocked_until = CASE
                    WHEN (
                        CASE
                            WHEN auth_rate_limits.window_started_at
                                 <= now() - (:window_seconds * interval '1 second')
                            THEN 1
                            ELSE auth_rate_limits.attempt_count + 1
                        END
                    ) >= :max_attempts
                    THEN now() + (:block_seconds * interval '1 second')
                    ELSE CASE
                        WHEN auth_rate_limits.blocked_until <= now()
                        THEN NULL
                        ELSE auth_rate_limits.blocked_until
                    END
                END,
                updated_at = now()
            """
        ),
        {
            "scope": policy.scope,
            "subject_hash": key_hash,
            "window_seconds": policy.window_seconds,
            "max_attempts": policy.max_attempts,
            "block_seconds": policy.block_seconds,
        },
    )
    session.commit()


def record_attempt(
    session: Session,
    *,
    policy: RateLimitPolicy,
    subject: str,
    fingerprint_secret: str,
) -> None:
    """Count a request even if the account does not exist (recovery anti-enumeration)."""
    record_failure(
        session,
        policy=policy,
        subject=subject,
        fingerprint_secret=fingerprint_secret,
    )


def clear_failures(
    session: Session,
    *,
    policy: RateLimitPolicy,
    subject: str,
    fingerprint_secret: str,
) -> None:
    key_hash = subject_hash(subject, fingerprint_secret)
    session.execute(
        text(
            """
            DELETE FROM auth_rate_limits
            WHERE scope=:scope AND subject_hash=:subject_hash
            """
        ),
        {"scope": policy.scope, "subject_hash": key_hash},
    )
    session.commit()


LOGIN_POLICY = RateLimitPolicy(
    scope="login",
    max_attempts=5,
    window_seconds=15 * 60,
    block_seconds=15 * 60,
)
RECOVERY_POLICY = RateLimitPolicy(
    scope="recovery",
    max_attempts=3,
    window_seconds=60 * 60,
    block_seconds=60 * 60,
)
ADMIN_SETUP_POLICY = RateLimitPolicy(
    scope="admin_setup",
    max_attempts=5,
    window_seconds=15 * 60,
    block_seconds=15 * 60,
)


__all__ = [
    "ADMIN_SETUP_POLICY",
    "LOGIN_POLICY",
    "RECOVERY_POLICY",
    "RateLimitExceeded",
    "RateLimitPolicy",
    "assert_not_limited",
    "clear_failures",
    "record_attempt",
    "record_failure",
    "subject_hash",
]
