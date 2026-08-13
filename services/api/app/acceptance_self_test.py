"""Temporary read-only Owner MT5 mirror for the genuine invited-user acceptance test.

This module exists only so the Owner can inspect the ordinary-member experience using the
already-connected Owner broker account. It never grants Owner permissions, never creates
an MT5/MetaAPI account for the invited user, and is never used by mutation/trading routes.
Disable it by clearing SUPER_SIGNALS_ACCEPTANCE_MIRROR_EMAIL after acceptance.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


def acceptance_mirror_enabled(identity: dict[str, Any]) -> bool:
    expected = os.getenv("SUPER_SIGNALS_ACCEPTANCE_MIRROR_EMAIL", "").strip().casefold()
    email = str(identity.get("email") or "").strip().casefold()
    return bool(expected and email == expected and str(identity.get("role") or "") == "user")


def acceptance_mirror_owner_user_id(
    identity: dict[str, Any],
    session_factory: sessionmaker[Session],
) -> UUID | None:
    """Return the active Owner ID only for the configured acceptance member."""

    if not acceptance_mirror_enabled(identity):
        return None
    with session_factory() as session:
        value = session.execute(
            text(
                """
                SELECT u.id
                FROM users u
                JOIN user_roles ur ON ur.user_id=u.id
                JOIN roles r ON r.id=ur.role_id
                WHERE r.name='owner' AND u.status='active'
                ORDER BY u.created_at,u.id
                LIMIT 1
                """
            )
        ).scalar_one_or_none()
    return value if isinstance(value, UUID) else None


__all__ = ["acceptance_mirror_enabled", "acceptance_mirror_owner_user_id"]
