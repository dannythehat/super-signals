"""Public Smart Signals account creation for the website onboarding flow.

A website signup creates an ordinary member identity in the same user database used by
the app. Each signup also creates a short-lived, one-time complimentary-access approval
token and emails the private owner destination when outbound email is configured.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_db_session
from app.member_email import send_admin_new_signup
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
    complimentary_token = secrets.token_urlsafe(36)
    token_hash = hashlib.sha256(complimentary_token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

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
    session.execute(
        text(
            """
            INSERT INTO complimentary_access_tokens (user_id, token_hash, expires_at)
            VALUES (:user_id, :token_hash, :expires_at)
            """
        ),
        {"user_id": user.id, "token_hash": token_hash, "expires_at": expires_at},
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
                "complimentary_owner_approval_available": True,
            },
        )
    )
    session.commit()

    delivery = send_admin_new_signup(
        member_email=email,
        display_name=display_name,
        complimentary_token=complimentary_token,
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_type=(
                "access.signup_admin_email_sent"
                if delivery.sent
                else "access.signup_admin_email_not_sent"
            ),
            entity_type="user",
            entity_id=user.id,
            payload={"reason": delivery.reason},
        )
    )
    session.commit()

    return PublicSignupResponse(email=email, display_name=display_name)
