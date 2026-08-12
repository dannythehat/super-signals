"""Owner-created, email-bound, single-use member invitations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session
from app.models import AuditEvent, Invitation, Role, User
from app.security import hash_token, new_token

router = APIRouter(prefix="/admin/invitations", tags=["invitations"])
DbSession = Annotated[Session, Depends(get_db_session)]
OwnerAccessKeys = Annotated[
    dict[str, Any],
    Depends(require_permission("access_keys.manage")),
]


class CreateInvitationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    expires_in_hours: int = Field(default=168, ge=1, le=720)


class CreatedInvitationResponse(BaseModel):
    id: UUID
    email: str
    access_key: str
    expires_at: datetime
    role: Literal["user"] = "user"


class InvitationView(BaseModel):
    id: UUID
    email: str
    status: Literal["active", "used", "expired", "revoked"]
    expires_at: datetime
    created_at: datetime


class RevokeInvitationResponse(BaseModel):
    id: UUID
    status: Literal["revoked"] = "revoked"


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid email address.",
        )
    return email


def _invitation_status(invitation: Invitation, now: datetime) -> str:
    if invitation.used_at is not None:
        return "used"
    if invitation.revoked_at is not None:
        return "revoked"
    if invitation.expires_at <= now:
        return "expired"
    return "active"


@router.get("", response_model=list[InvitationView])
def list_invitations(
    session: DbSession,
    actor: OwnerAccessKeys,
) -> list[InvitationView]:
    del actor
    now = datetime.now(UTC)
    rows = session.scalars(
        select(Invitation).order_by(Invitation.created_at.desc()).limit(200)
    ).all()
    return [
        InvitationView(
            id=row.id,
            email=str(row.email),
            status=_invitation_status(row, now),
            expires_at=row.expires_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post("", response_model=CreatedInvitationResponse, status_code=status.HTTP_201_CREATED)
def create_invitation(
    payload: CreateInvitationRequest,
    session: DbSession,
    actor: OwnerAccessKeys,
) -> CreatedInvitationResponse:
    email = _normalize_email(payload.email)
    now = datetime.now(UTC)

    existing_user = session.scalar(select(User).where(User.email == email))
    if existing_user is not None and existing_user.status == "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That email already has an active account.",
        )

    role = session.scalar(select(Role).where(Role.name == "user"))
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invited User role is not configured.",
        )

    # Only one live key per approved email. Creating a replacement revokes any
    # earlier unused key so the owner never has to reason about multiple valid keys.
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
                actor_user_id=actor["id"],
                event_type="access.invitation_replaced",
                entity_type="invitation",
                entity_id=row.id,
                payload={"email": email},
            )
        )

    raw_key = new_token()
    invitation = Invitation(
        email=email,
        key_hash=hash_token(raw_key),
        role_id=role.id,
        created_by_user_id=actor["id"],
        expires_at=now + timedelta(hours=payload.expires_in_hours),
    )
    session.add(invitation)
    session.flush()
    session.add(
        AuditEvent(
            actor_user_id=actor["id"],
            event_type="access.invitation_created",
            entity_type="invitation",
            entity_id=invitation.id,
            payload={
                "email": email,
                "role": "user",
                "expires_at": invitation.expires_at.isoformat(),
            },
        )
    )
    session.commit()
    session.refresh(invitation)

    return CreatedInvitationResponse(
        id=invitation.id,
        email=email,
        access_key=raw_key,
        expires_at=invitation.expires_at,
    )


@router.post("/{invitation_id}/revoke", response_model=RevokeInvitationResponse)
def revoke_invitation(
    invitation_id: UUID,
    session: DbSession,
    actor: OwnerAccessKeys,
) -> RevokeInvitationResponse:
    invitation = session.get(Invitation, invitation_id)
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation was not found.",
        )
    if invitation.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A used invitation cannot be revoked.",
        )

    if invitation.revoked_at is None:
        invitation.revoked_at = datetime.now(UTC)
        session.add(
            AuditEvent(
                actor_user_id=actor["id"],
                event_type="access.invitation_revoked",
                entity_type="invitation",
                entity_id=invitation.id,
                payload={"email": str(invitation.email)},
            )
        )
        session.commit()

    return RevokeInvitationResponse(id=invitation.id)
