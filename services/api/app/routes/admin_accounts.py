"""Owner-only administrator provisioning for live acceptance and platform operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session
from app.models import AuditEvent, Role, User, UserRole
from app.security import hash_token, new_token

router = APIRouter(prefix="/admin/accounts", tags=["admin accounts"])
DbSession = Annotated[Session, Depends(get_db_session)]
OwnerAdmins = Annotated[dict[str, Any], Depends(require_permission("admins.manage"))]


class TradingAdminSetupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="Trading Admin", min_length=1, max_length=120)


class TradingAdminSetupResponse(BaseModel):
    email: str
    display_name: str
    role: str = "trading_admin"
    setup_token: str
    expires_at: datetime


@router.post("/trading-admins/setup", response_model=TradingAdminSetupResponse)
def create_trading_admin_setup(
    payload: TradingAdminSetupRequest,
    session: DbSession,
    actor: OwnerAdmins,
) -> TradingAdminSetupResponse:
    normalized_email = payload.email.strip().lower()
    if "@" not in normalized_email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid email address.",
        )

    role = session.scalar(select(Role).where(Role.name == "trading_admin"))
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Trading Admin role is not configured.",
        )

    account = session.scalar(select(User).where(User.email == normalized_email))
    if account is None:
        account = User(
            email=normalized_email,
            display_name=payload.display_name.strip(),
            status="invited",
        )
        session.add(account)
        session.flush()
    else:
        existing_owner_role = session.scalar(
            select(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == account.id, Role.name == "owner")
        )
        if existing_owner_role is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That account is already an Owner Admin.",
            )
        account.display_name = payload.display_name.strip() or account.display_name
        if account.status in {"suspended", "revoked"}:
            account.status = "invited"

    link = session.scalar(
        select(UserRole).where(UserRole.user_id == account.id, UserRole.role_id == role.id)
    )
    if link is None:
        session.add(
            UserRole(
                user_id=account.id,
                role_id=role.id,
                granted_by_user_id=actor["id"],
            )
        )

    session.execute(
        text(
            """
            UPDATE password_recovery_requests
            SET used_at = COALESCE(used_at, now())
            WHERE user_id = :user_id
              AND used_at IS NULL
            """
        ),
        {"user_id": account.id},
    )

    raw_token = new_token()
    expires_at = datetime.now(UTC) + timedelta(hours=2)
    session.execute(
        text(
            """
            INSERT INTO password_recovery_requests (
                user_id, token_hash, requested_ip_hash, expires_at
            )
            VALUES (:user_id, :token_hash, NULL, :expires_at)
            """
        ),
        {
            "user_id": account.id,
            "token_hash": hash_token(raw_token),
            "expires_at": expires_at,
        },
    )
    session.add(
        AuditEvent(
            actor_user_id=actor["id"],
            event_type="admin.trading_admin_setup_created",
            entity_type="user",
            entity_id=account.id,
            payload={
                "email": normalized_email,
                "display_name": account.display_name,
                "role": "trading_admin",
                "expires_at": expires_at.isoformat(),
            },
        )
    )
    session.commit()

    return TradingAdminSetupResponse(
        email=normalized_email,
        display_name=account.display_name or "Trading Admin",
        setup_token=raw_token,
        expires_at=expires_at,
    )
