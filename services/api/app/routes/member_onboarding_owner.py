"""Owner-only one-step onboarding for ordinary live Smart Signals members."""

from __future__ import annotations

import secrets
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session, get_session_factory
from app.member_email import send_member_live_welcome
from app.models import AuditEvent, Role, User, UserRole
from app.mt5_account_profiles import Mt5AccountProfileService
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.routes.admin_accounts import router
from app.security import hash_password

DbSession = Annotated[Session, Depends(get_db_session)]
OwnerUsers = Annotated[dict[str, Any], Depends(require_permission("users.manage"))]


class OnboardLiveMemberRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=120)
    mt5_login: str = Field(min_length=1, max_length=32)
    mt5_password: SecretStr
    mt5_server: str = Field(min_length=2, max_length=160)
    complimentary_access: bool = True


class OnboardLiveMemberResponse(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    created: bool
    complimentary_access: bool
    mt5_login_masked: str | None
    mt5_server: str | None
    mt5_status: str
    remote_state: str | None
    remote_connection_status: str | None
    trading_status: str
    risk_percent: float
    ready: bool
    welcome_email_sent: bool
    welcome_email_reason: str | None = None


def _owner(identity: dict[str, Any]) -> None:
    if identity.get("role") != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "owner_required", "message": "Only the Owner can onboard a live member."},
        )


def _service(request: Request) -> Day30Mt5ConnectionService:
    service = require_mt5_service(request)
    if not isinstance(service, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "mt5_user_linking_not_configured", "message": "Live MT5 onboarding is unavailable right now."},
        )
    return service


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return email


def _prepare_member(
    session: Session,
    *,
    owner_id: UUID,
    email: str,
    display_name: str,
    complimentary_access: bool,
) -> tuple[UUID, bool]:
    role = session.scalar(select(Role).where(Role.name == "user"))
    if role is None:
        raise HTTPException(status_code=500, detail="User role is not configured.")

    account = session.scalar(select(User).where(User.email == email))
    created = account is None
    if account is None:
        account = User(email=email, display_name=display_name, status="active")
        session.add(account)
        session.flush()
        session.execute(
            text("UPDATE users SET password_hash=:password_hash WHERE id=:user_id"),
            {
                "password_hash": hash_password(secrets.token_urlsafe(48)),
                "user_id": account.id,
            },
        )
        session.add(UserRole(user_id=account.id, role_id=role.id, granted_by_user_id=owner_id))
    else:
        existing_roles = set(
            session.scalars(
                select(Role.name)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == account.id)
            ).all()
        )
        if existing_roles - {"user"}:
            raise HTTPException(status_code=409, detail="That email belongs to a privileged account and cannot be onboarded as a member.")
        exposure = int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM positions WHERE user_id=:user_id AND status IN ('open','pending')"
                ),
                {"user_id": account.id},
            ).scalar_one()
        )
        if exposure:
            raise HTTPException(status_code=409, detail="This member has open or pending mapped trades. Finish those trades before changing the live MT5 setup.")
        account.display_name = display_name
        account.status = "active"
        if "user" not in existing_roles:
            session.add(UserRole(user_id=account.id, role_id=role.id, granted_by_user_id=owner_id))

    if complimentary_access:
        session.execute(
            text(
                """
                INSERT INTO complimentary_access_grants
                    (user_id, status, granted_by_user_id, source, granted_at, revoked_at, updated_at)
                VALUES
                    (:user_id, 'active', :owner_id, 'owner_one_step_onboarding', now(), NULL, now())
                ON CONFLICT (user_id) DO UPDATE SET
                    status='active',
                    granted_by_user_id=EXCLUDED.granted_by_user_id,
                    source=EXCLUDED.source,
                    granted_at=EXCLUDED.granted_at,
                    revoked_at=NULL,
                    updated_at=EXCLUDED.updated_at
                """
            ),
            {"user_id": account.id, "owner_id": owner_id},
        )

    # Fail safe while the broker connection is being verified. The final activation
    # happens only after MetaAPI reports a connected live account.
    session.execute(
        text(
            """
            INSERT INTO user_trading_controls
                (user_id, risk_percent, allow_double_lot, trading_status, activated_at, stopped_at, updated_at)
            VALUES
                (:user_id, 1.0, FALSE, 'stopped', NULL, now(), now())
            ON CONFLICT (user_id) DO UPDATE SET
                risk_percent=1.0,
                allow_double_lot=FALSE,
                trading_status='stopped',
                stopped_at=now(),
                updated_at=now()
            """
        ),
        {"user_id": account.id},
    )
    session.add(
        AuditEvent(
            actor_user_id=owner_id,
            event_type="access.member_onboarding_started",
            entity_type="user",
            entity_id=account.id,
            payload={
                "email": email,
                "display_name": display_name,
                "created": created,
                "complimentary_access": complimentary_access,
                "risk_percent": 1.0,
                "allow_double_lot": False,
                "trading_status": "stopped_pending_mt5_verification",
            },
        )
    )
    session.commit()
    return account.id, created


def _activate_verified_member(session: Session, *, owner_id: UUID, user_id: UUID) -> None:
    session.execute(
        text(
            """
            UPDATE user_trading_controls
            SET risk_percent=1.0,
                allow_double_lot=FALSE,
                trading_status='active',
                activated_at=now(),
                stopped_at=NULL,
                updated_at=now()
            WHERE user_id=:user_id
            """
        ),
        {"user_id": user_id},
    )
    session.add(
        AuditEvent(
            actor_user_id=owner_id,
            event_type="access.member_onboarding_ready",
            entity_type="user",
            entity_id=user_id,
            payload={"risk_percent": 1.0, "allow_double_lot": False, "trading_status": "active"},
        )
    )
    session.commit()


@router.post("/members/onboard-live", response_model=OnboardLiveMemberResponse)
async def onboard_live_member(
    payload: OnboardLiveMemberRequest,
    request: Request,
    response: Response,
    session: DbSession,
    identity: OwnerUsers,
) -> OnboardLiveMemberResponse:
    _owner(identity)
    email = _normalize_email(payload.email)
    display_name = payload.display_name.strip()
    service = _service(request)
    user_id, created = _prepare_member(
        session,
        owner_id=identity["id"],
        email=email,
        display_name=display_name,
        complimentary_access=payload.complimentary_access,
    )

    try:
        service.approve_user_account(
            approver_user_id=identity["id"],
            user_id=user_id,
            login=payload.mt5_login,
            server=payload.mt5_server,
        )
        view = await service.connect_user_live(
            user_id=user_id,
            login=payload.mt5_login,
            password=payload.mt5_password.get_secret_value(),
            server=payload.mt5_server,
        )
    except Mt5ConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": "The member account was created safely, but the live Vantage MT5 connection was not verified. Trading remains stopped."},
        ) from exc

    profiles = Mt5AccountProfileService(
        session_factory=get_session_factory(),
        connection_service=service,
    )
    profiles.sync_active_profile_from_canonical(user_id)
    ready = str(view.status).lower() == "connected" and str(view.remote_connection_status or "").lower() == "connected"
    if ready:
        _activate_verified_member(session, owner_id=identity["id"], user_id=user_id)

    welcome_sent = False
    welcome_reason: str | None = "mt5_not_connected" if not ready else None
    if ready:
        already_sent = bool(
            session.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM audit_events WHERE entity_id=:user_id AND event_type='access.live_welcome_email_sent')"
                ),
                {"user_id": user_id},
            ).scalar_one()
        )
        if already_sent:
            welcome_sent = True
            welcome_reason = "already_sent"
        else:
            delivery = send_member_live_welcome(
                member_email=email,
                display_name=display_name,
                login_masked=view.login_masked or "Connected",
                server=view.server or payload.mt5_server.strip(),
            )
            welcome_sent = delivery.sent
            welcome_reason = delivery.reason
            session.add(
                AuditEvent(
                    actor_user_id=identity["id"],
                    event_type=("access.live_welcome_email_sent" if delivery.sent else "access.live_welcome_email_not_sent"),
                    entity_type="user",
                    entity_id=user_id,
                    payload={"reason": delivery.reason, "sender": "smart_signals_transactional"},
                )
            )
            session.commit()

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return OnboardLiveMemberResponse(
        user_id=user_id,
        email=email,
        display_name=display_name,
        created=created,
        complimentary_access=payload.complimentary_access,
        mt5_login_masked=view.login_masked,
        mt5_server=view.server,
        mt5_status=view.status,
        remote_state=view.remote_state,
        remote_connection_status=view.remote_connection_status,
        trading_status="active" if ready else "stopped",
        risk_percent=1.0,
        ready=ready,
        welcome_email_sent=welcome_sent,
        welcome_email_reason=welcome_reason,
    )
