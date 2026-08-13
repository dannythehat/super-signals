"""One-time controlled invitation bootstrap for Danny's Day 35 member acceptance.

Only a SHA-256 access-key hash is supplied through Render. The raw key never
enters GitHub, Render environment variables, application logs or PostgreSQL.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import AuditEvent, Invitation, Role, User


def main() -> None:
    email = os.getenv("SUPER_SIGNALS_ACCEPTANCE_INVITE_EMAIL", "").strip().lower()
    key_hash = os.getenv("SUPER_SIGNALS_ACCEPTANCE_INVITE_KEY_HASH", "").strip().lower()

    if not email and not key_hash:
        print("Acceptance invitation: no request")
        return
    if not email or not key_hash:
        raise RuntimeError("Acceptance invitation requires email and key hash")
    if "@" not in email:
        raise RuntimeError("Acceptance invitation email is invalid")
    if len(key_hash) != 64 or any(ch not in "0123456789abcdef" for ch in key_hash):
        raise RuntimeError("Acceptance invitation key hash must be SHA-256 hex")

    now = datetime.now(UTC)
    with Session(get_engine()) as session:
        owner_role = session.scalar(select(Role).where(Role.name == "owner"))
        user_role = session.scalar(select(Role).where(Role.name == "user"))
        if owner_role is None or user_role is None:
            raise RuntimeError("Required roles are not configured")

        owner = session.scalar(
            select(User)
            .join(owner_role.user_links)
            .where(User.email == os.getenv("SUPER_SIGNALS_OWNER_EMAIL", "").strip().lower())
        )
        if owner is None or owner.status != "active":
            raise RuntimeError("Active configured Owner was not found")

        existing_user = session.scalar(select(User).where(User.email == email))
        if existing_user is not None and existing_user.status == "active":
            print(f"Acceptance invitation: active account already exists for {email}")
            return

        existing_same_hash = session.scalar(select(Invitation).where(Invitation.key_hash == key_hash))
        if existing_same_hash is not None:
            print(f"Acceptance invitation: already created for {email}")
            return

        active_rows = session.scalars(
            select(Invitation).where(
                Invitation.email == email,
                Invitation.used_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > now,
            )
        ).all()
        for row in active_rows:
            row.revoked_at = now
            session.add(
                AuditEvent(
                    actor_user_id=owner.id,
                    event_type="access.invitation_replaced",
                    entity_type="invitation",
                    entity_id=row.id,
                    payload={"email": email, "acceptance_self_test": True},
                )
            )

        invitation = Invitation(
            email=email,
            key_hash=key_hash,
            role_id=user_role.id,
            created_by_user_id=owner.id,
            expires_at=now + timedelta(days=7),
        )
        session.add(invitation)
        session.flush()
        session.add(
            AuditEvent(
                actor_user_id=owner.id,
                event_type="access.invitation_created",
                entity_type="invitation",
                entity_id=invitation.id,
                payload={
                    "email": email,
                    "role": "user",
                    "expires_at": invitation.expires_at.isoformat(),
                    "acceptance_self_test": True,
                    "affiliate_gate_deferred_for_owner_self_test": True,
                },
            )
        )
        session.commit()
        print(f"Acceptance invitation: created for {email} ({invitation.id})")


if __name__ == "__main__":
    main()
