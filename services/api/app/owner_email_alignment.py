"""One-time controlled Owner email alignment for the live Super Signals account.

This utility preserves the existing Owner user identity and only changes its email.
It is intentionally fail-closed and idempotent. It runs only when both migration
environment variables are populated.
"""

from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import AuditEvent, Role, User, UserRole


def _has_owner_role(session: Session, user_id) -> bool:
    return (
        session.scalar(
            select(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == user_id, Role.name == "owner")
        )
        is not None
    )


def main() -> None:
    source_email = os.getenv("SUPER_SIGNALS_OWNER_EMAIL_MIGRATE_FROM", "").strip().lower()
    target_email = os.getenv("SUPER_SIGNALS_OWNER_EMAIL_MIGRATE_TO", "").strip().lower()

    if not source_email and not target_email:
        print("Owner email alignment: no migration requested")
        return
    if not source_email or not target_email:
        raise RuntimeError("Owner email alignment requires both source and target emails")
    if source_email == target_email:
        print("Owner email alignment: source and target are identical; no change needed")
        return

    with Session(get_engine()) as session:
        source = session.scalar(select(User).where(User.email == source_email))
        target = session.scalar(select(User).where(User.email == target_email))

        if source is None:
            if target is not None and _has_owner_role(session, target.id):
                print("Owner email alignment: already completed")
                return
            raise RuntimeError("Owner email alignment source account was not found")

        if not _has_owner_role(session, source.id):
            raise RuntimeError("Owner email alignment source account is not an Owner")
        if target is not None and target.id != source.id:
            raise RuntimeError("Owner email alignment target email is already in use")

        previous_email = str(source.email)
        source.email = target_email
        session.add(
            AuditEvent(
                actor_user_id=source.id,
                event_type="admin.owner_email_aligned",
                entity_type="user",
                entity_id=source.id,
                payload={
                    "from_email": previous_email,
                    "to_email": target_email,
                    "method": "owner_authorized_controlled_render_migration",
                },
            )
        )
        session.commit()
        print("Owner email alignment: completed")


if __name__ == "__main__":
    main()
