"""Public registration using an owner-approved email and one-time access key."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_db_session
from app.models import AuditEvent, Invitation, Role, User, UserRole
from app.security import hash_password, hash_token

router = APIRouter(prefix="/auth", tags=["authentication"])
DbSession = Annotated[Session, Depends(get_db_session)]

_GENERIC_INVITATION_ERROR = (
    "Invitation email or access key is invalid, expired, revoked or already used."
)


class InvitationRegistrationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    access_key: str = Field(min_length=20, max_length=256)
    password: str = Field(min_length=12, max_length=128)
    display_name: str | None = Field(default=None, max_length=120)


class InvitationRegistrationResponse(BaseModel):
    email: str
    message: str = "Account created. You can sign in now."


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _reject(
    session: Session,
    *,
    email: str,
    code: str,
    invitation_id=None,
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=None,
            event_type="access.invitation_registration_rejected",
            entity_type="invitation",
            entity_id=invitation_id,
            payload={"email": email, "reason": code},
        )
    )
    session.commit()
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=_GENERIC_INVITATION_ERROR,
    )


@router.post(
    "/register",
    response_model=InvitationRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_with_invitation(
    payload: InvitationRegistrationRequest,
    session: DbSession,
) -> InvitationRegistrationResponse:
    email = _normalize_email(payload.email)
    key_hash = hash_token(payload.access_key)
    now = datetime.now(UTC)

    # Row locking makes the one-time property true under concurrency: only the
    # first request can mark this key used; the second waits and then sees used_at.
    invitation = session.scalar(
        select(Invitation)
        .where(Invitation.key_hash == key_hash)
        .with_for_update()
    )
    if invitation is None:
        _reject(session, email=email, code="invalid_key")

    invitation_id = invitation.id
    if str(invitation.email).strip().lower() != email:
        _reject(
            session,
            email=email,
            code="wrong_email",
            invitation_id=invitation_id,
        )
    if invitation.used_at is not None:
        _reject(
            session,
            email=email,
            code="reused_key",
            invitation_id=invitation_id,
        )
    if invitation.revoked_at is not None:
        _reject(
            session,
            email=email,
            code="revoked_key",
            invitation_id=invitation_id,
        )
    if invitation.expires_at <= now:
        _reject(
            session,
            email=email,
            code="expired_key",
            invitation_id=invitation_id,
        )

    role = session.get(Role, invitation.role_id)
    if role is None or role.name != "user":
        _reject(
            session,
            email=email,
            code="unsupported_role",
            invitation_id=invitation_id,
        )

    existing_user = session.scalar(select(User).where(User.email == email))
    if existing_user is not None:
        _reject(
            session,
            email=email,
            code="account_exists",
            invitation_id=invitation_id,
        )

    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    display_name = (payload.display_name or "").strip() or email.split("@", 1)[0]
    user = User(
        email=email,
        display_name=display_name[:120],
        status="active",
    )
    session.add(user)
    session.flush()
    session.execute(
        text("UPDATE users SET password_hash = :password_hash WHERE id = :user_id"),
        {"password_hash": password_hash, "user_id": user.id},
    )
    session.add(
        UserRole(
            user_id=user.id,
            role_id=role.id,
            granted_by_user_id=invitation.created_by_user_id,
        )
    )
    invitation.used_at = now
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="access.invitation_registered",
            entity_type="invitation",
            entity_id=invitation.id,
            payload={
                "email": email,
                "user_id": str(user.id),
                "role": "user",
            },
        )
    )
    session.commit()

    return InvitationRegistrationResponse(email=email)
