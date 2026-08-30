"""Public Smart Signals account creation for the website onboarding flow.

This route deliberately leaves the existing owner-invitation registration path
untouched. A website signup creates an ordinary member identity in the same user
database used by the app; broker connectivity and trading remain governed by
their existing approval/control layers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_db_session
from app.models import AuditEvent, Role, User, UserRole
from app.routes.auth import router
from app.security import hash_password

DbSession = Annotated[Session, Depends(get_db_session)]


class PublicSignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=128)
    display_name: str | None = Field(default=None, max_length=120)


class PublicSignupResponse(BaseModel):
    email: str
    display_name: str
    message: str = "Smart Signals account created."


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if (
        "@" not in email
        or email.startswith("@")
        or email.endswith("@")
        or "." not in email.rsplit("@", 1)[-1]
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid email address.",
        )
    return email


@router.post(
    "/signup",
    response_model=PublicSignupResponse,
    status_code=status.HTTP_201_CREATED,
)
def public_signup(
    payload: PublicSignupRequest,
    session: DbSession,
) -> PublicSignupResponse:
    email = _normalize_email(payload.email)

    existing = session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account already exists for this email. Sign in instead.",
        )

    role = session.scalar(select(Role).where(Role.name == "user"))
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Member signup is temporarily unavailable.",
        )

    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    requested_name = (payload.display_name or "").strip()
    display_name = (requested_name or email.split("@", 1)[0])[:120]

    user = User(
        email=email,
        display_name=display_name,
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
            granted_by_user_id=None,
        )
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type="access.public_signup_created",
            entity_type="user",
            entity_id=user.id,
            payload={
                "email": email,
                "role": "user",
                "source": "website_onboarding",
                "trade_action_created": False,
            },
        )
    )
    session.commit()

    return PublicSignupResponse(email=email, display_name=display_name)
