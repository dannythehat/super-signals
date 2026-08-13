"""One-time controlled Owner email alignment for the live Super Signals account.

This utility preserves the existing Owner user identity and only changes its email.
It is intentionally fail-closed and idempotent. It also handles the specific
bootstrap edge case where the old configured email was re-seeded after the
first alignment by retiring that duplicate account safely.
"""

from __future__ import annotations

import os

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import AuditEvent, Role, User, UserRole


def _owner_link(session: Session, user_id) -> UserRole | None:
    return session.scalar(
        select(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user_id, Role.name == "owner")
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
            if target is not None and _owner_link(session, target.id) is not None:
                print("Owner email alignment: already completed")
                return
            raise RuntimeError("Owner email alignment source account was not found")

        source_owner_link = _owner_link(session, source.id)
        if source_owner_link is None:
            raise RuntimeError("Owner email alignment source account is not an Owner")

        if target is not None and target.id != source.id:
            if _owner_link(session, target.id) is None:
                raise RuntimeError("Owner email alignment target email is already in use by a non-Owner")

            # The first alignment can be followed by bootstrap re-seeding the old
            # configured Owner email. In that exact state, keep the migrated target
            # Owner and retire the newly re-seeded source account. This removes its
            # Owner permission and revokes any sessions before bootstrap runs again.
            source.status = "revoked"
            session.execute(
                text(
                    """
                    UPDATE auth_sessions
                    SET revoked_at = COALESCE(revoked_at, now())
                    WHERE user_id = :user_id
                      AND revoked_at IS NULL
                    """
                ),
                {"user_id": source.id},
            )
            session.delete(source_owner_link)
            session.add(
                AuditEvent(
                    actor_user_id=target.id,
                    event_type="admin.duplicate_owner_retired",
                    entity_type="user",
                    entity_id=source.id,
                    payload={
                        "retired_email": source_email,
                        "retained_owner_email": target_email,
                        "reason": "old_owner_email_reseeded_after_controlled_alignment",
                    },
                )
            )
            session.commit()
            print("Owner email alignment: re-seeded duplicate Owner retired")
            return

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
