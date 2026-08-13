"""Database-backed throttling for public authentication surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.security import privacy_hash


@dataclass(frozen=True, slots=True)
class RateLimitExceeded(Exception):
    retry_after_seconds: int

    def __str__(self) -> str:
        return "Too many attempts. Try again later."


def _now() -> datetime:
    return datetime.now(UTC)


def _key_hash(scope: str, key_material: str, secret: str) -> str:
    normalized = key_material.strip().lower()
    value = privacy_hash(f"{scope}:{normalized}", secret)
    if value is None:
        raise ValueError("Rate-limit key material must not be empty.")
    return value


def check_rate_limit(
    session: Session,
    *,
    scope: str,
    key_material: str,
    secret: str,
) -> None:
    """Fail before sensitive work when an existing bucket is still blocked."""
    key_hash = _key_hash(scope, key_material, secret)
    now = _now()
    blocked_until = session.scalar(
        text(
            """
            SELECT blocked_until
            FROM auth_rate_limits
            WHERE scope = :scope
              AND key_hash = :key_hash
            """
        ),
        {"scope": scope, "key_hash": key_hash},
    )
    if blocked_until is not None and blocked_until > now:
        retry = max(1, int((blocked_until - now).total_seconds()))
        raise RateLimitExceeded(retry)


def record_rate_limit_failure(
    session: Session,
    *,
    scope: str,
    key_material: str,
    secret: str,
    limit: int,
    window_seconds: int,
    block_seconds: int,
) -> None:
    """Record one failed/sensitive attempt without storing the raw bucket key."""
    if limit <= 0 or window_seconds <= 0 or block_seconds <= 0:
        raise ValueError("Rate-limit settings must be positive.")

    key_hash = _key_hash(scope, key_material, secret)
    now = _now()
    session.execute(
        text(
            """
            INSERT INTO auth_rate_limits (
                scope, key_hash, window_started_at, attempt_count, updated_at
            )
            VALUES (:scope, :key_hash, :now, 0, :now)
            ON CONFLICT (scope, key_hash) DO NOTHING
            """
        ),
        {"scope": scope, "key_hash": key_hash, "now": now},
    )
    row = (
        session.execute(
            text(
                """
                SELECT window_started_at, attempt_count, blocked_until
                FROM auth_rate_limits
                WHERE scope = :scope
                  AND key_hash = :key_hash
                FOR UPDATE
                """
            ),
            {"scope": scope, "key_hash": key_hash},
        )
        .mappings()
        .one()
    )

    if row["blocked_until"] is not None and row["blocked_until"] > now:
        session.commit()
        return

    window_started_at = row["window_started_at"]
    if window_started_at + timedelta(seconds=window_seconds) <= now:
        attempt_count = 1
        window_started_at = now
    else:
        attempt_count = int(row["attempt_count"]) + 1

    blocked_until = (
        now + timedelta(seconds=block_seconds)
        if attempt_count >= limit
        else None
    )
    session.execute(
        text(
            """
            UPDATE auth_rate_limits
            SET window_started_at = :window_started_at,
                attempt_count = :attempt_count,
                blocked_until = :blocked_until,
                updated_at = :now
            WHERE scope = :scope
              AND key_hash = :key_hash
            """
        ),
        {
            "window_started_at": window_started_at,
            "attempt_count": attempt_count,
            "blocked_until": blocked_until,
            "now": now,
            "scope": scope,
            "key_hash": key_hash,
        },
    )
    session.commit()


def clear_rate_limit(
    session: Session,
    *,
    scope: str,
    key_material: str,
    secret: str,
) -> None:
    """Clear only the successful account-specific bucket, not a wider IP bucket."""
    key_hash = _key_hash(scope, key_material, secret)
    session.execute(
        text(
            """
            DELETE FROM auth_rate_limits
            WHERE scope = :scope
              AND key_hash = :key_hash
            """
        ),
        {"scope": scope, "key_hash": key_hash},
    )
    session.commit()
