"""Temporary one-shot Day 29 acceptance against Render Postgres.

Inert unless SUPER_SIGNALS_DAY29_LIVE_ACCEPTANCE=1. Raw invitation keys are never
logged or persisted outside the normal SHA-256 invitation hash.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from app.db import get_session_factory
from app.models import AuditEvent, Role, User
from app.routes.invitations import (
    CreateInvitationRequest,
    create_invitation,
    revoke_invitation,
)
from app.routes.registration import (
    InvitationRegistrationRequest,
    register_with_invitation,
)

logger = logging.getLogger(__name__)


def _expect_rejection(session, *, email: str, access_key: str) -> None:
    try:
        register_with_invitation(
            InvitationRegistrationRequest(
                email=email,
                access_key=access_key,
                password="day29 acceptance password",
                display_name="Day 29 Acceptance",
            ),
            session,
        )
    except HTTPException as exc:
        if exc.status_code != 400:
            raise AssertionError(f"unexpected rejection status {exc.status_code}") from exc
        return
    raise AssertionError("expected invitation registration rejection")


def run_day29_live_acceptance() -> None:
    factory = get_session_factory()
    marker = uuid4().hex[:12]
    success_email = f"day29-success-{marker}@example.invalid"
    expired_email = f"day29-expired-{marker}@example.invalid"
    revoked_email = f"day29-revoked-{marker}@example.invalid"
    wrong_email = f"day29-wrong-{marker}@example.invalid"

    with factory() as session:
        already = session.scalar(
            text(
                """
                SELECT count(*)
                FROM audit_events
                WHERE event_type = 'day29.live_acceptance_completed'
                  AND created_at > now() - interval '12 hours'
                """
            )
        )
        if already:
            logger.info("Day 29 live acceptance already completed recently; skipping")
            return

        owner = session.scalar(
            select(User)
            .join(text("user_roles ur"), text("ur.user_id = users.id"))
            .join(text("roles r"), text("r.id = ur.role_id"))
            .where(text("r.name = 'owner'"))
            .limit(1)
        )
        if owner is None:
            raise RuntimeError("Day 29 acceptance requires an owner account")
        user_role = session.scalar(select(Role).where(Role.name == "user"))
        if user_role is None:
            raise RuntimeError("Day 29 acceptance requires the user role")

        actor = {
            "id": owner.id,
            "role": "owner",
            "display_name": "Day 29 acceptance",
        }

        success_invite = create_invitation(
            CreateInvitationRequest(email=success_email, expires_in_hours=24),
            session,
            actor,
        )
        stored_hash = session.scalar(
            text("SELECT key_hash FROM invitations WHERE id = :id"),
            {"id": success_invite.id},
        )
        if stored_hash == success_invite.access_key or success_invite.access_key in str(stored_hash):
            raise AssertionError("raw invitation key was persisted")

        _expect_rejection(
            session,
            email=wrong_email,
            access_key=success_invite.access_key,
        )
        registered = register_with_invitation(
            InvitationRegistrationRequest(
                email=success_email.upper(),
                access_key=success_invite.access_key,
                password="day29 acceptance password",
                display_name="Day 29 Acceptance",
            ),
            session,
        )
        if registered.email != success_email:
            raise AssertionError("successful registration email was not normalized")
        _expect_rejection(
            session,
            email=success_email,
            access_key=success_invite.access_key,
        )

        expired_invite = create_invitation(
            CreateInvitationRequest(email=expired_email, expires_in_hours=24),
            session,
            actor,
        )
        session.execute(
            text("UPDATE invitations SET expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": expired_invite.id},
        )
        session.commit()
        _expect_rejection(
            session,
            email=expired_email,
            access_key=expired_invite.access_key,
        )

        revoked_invite = create_invitation(
            CreateInvitationRequest(email=revoked_email, expires_in_hours=24),
            session,
            actor,
        )
        revoke_invitation(revoked_invite.id, session, actor)
        _expect_rejection(
            session,
            email=revoked_email,
            access_key=revoked_invite.access_key,
        )

        reasons = set(
            session.execute(
                text(
                    """
                    SELECT payload->>'reason'
                    FROM audit_events
                    WHERE event_type = 'access.invitation_registration_rejected'
                      AND payload->>'email' IN (:success_email, :expired_email, :revoked_email, :wrong_email)
                    """
                ),
                {
                    "success_email": success_email,
                    "expired_email": expired_email,
                    "revoked_email": revoked_email,
                    "wrong_email": wrong_email,
                },
            ).scalars()
        )
        required = {"wrong_email", "reused_key", "expired_key", "revoked_key"}
        if not required.issubset(reasons):
            raise AssertionError(f"missing audited rejection reasons: {sorted(required - reasons)}")

        user_row = session.execute(
            text(
                """
                SELECT u.id, u.status, r.name AS role_name
                FROM users u
                JOIN user_roles ur ON ur.user_id = u.id
                JOIN roles r ON r.id = ur.role_id
                WHERE lower(u.email::text) = lower(:email)
                """
            ),
            {"email": success_email},
        ).mappings().one()
        if user_row["status"] != "active" or user_row["role_name"] != "user":
            raise AssertionError("successful invite did not create an active user-role account")

        session.execute(
            text("UPDATE users SET status = 'revoked', updated_at = now() WHERE id = :id"),
            {"id": user_row["id"]},
        )
        session.add(
            AuditEvent(
                actor_user_id=owner.id,
                event_type="day29.live_acceptance_completed",
                entity_type="invitation",
                entity_id=success_invite.id,
                payload={
                    "acceptance_version": "day29-live-2026-08-12",
                    "correct_email_registered_once": True,
                    "wrong_email_rejected": True,
                    "reused_key_rejected": True,
                    "expired_key_rejected": True,
                    "revoked_key_rejected": True,
                    "all_rejections_audited": True,
                    "raw_key_persisted": False,
                    "temporary_account_revoked": True,
                },
            )
        )
        session.commit()

    logger.info(
        "Day 29 LIVE acceptance PASSED: correct_once=true wrong_email=true reused=true expired=true revoked=true raw_key_persisted=false"
    )
