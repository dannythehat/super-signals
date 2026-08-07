"""Idempotent hosted-environment bootstrap for the first owner account."""

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


def main() -> None:
    email = os.getenv("SUPER_SIGNALS_OWNER_EMAIL", "").strip()
    password = os.getenv("SUPER_SIGNALS_OWNER_PASSWORD", "")
    display_name = os.getenv("SUPER_SIGNALS_OWNER_DISPLAY_NAME", "Owner").strip() or "Owner"

    if not email:
        raise RuntimeError("SUPER_SIGNALS_OWNER_EMAIL must be configured")
    if not password:
        raise RuntimeError("SUPER_SIGNALS_OWNER_PASSWORD must be configured")

    with Session(get_engine()) as session:
        changed = ensure_owner_credentials(session, email, password, display_name)

    status = "created or rotated" if changed else "already configured"
    print(f"Owner account {status}: {email}")


if __name__ == "__main__":
    main()
