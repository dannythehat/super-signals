"""Owner-only one-step onboarding for complimentary live Smart Signals members."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session, get_session_factory
from app.metaapi_gateway import MetaApiGatewayError, SUPER_SIGNALS_MAGIC
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
    complimentary_access: Literal[True] = True


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
            detail={"code": "owner_required", "message": "Only the Owner can onboard a complimentary live member."},
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


def _is_connected(view: Any) -> bool:
    return (
        str(view.status).lower() == "connected"
        and str(view.remote_connection_status or "").lower() == "connected"
    )


def _prepare_member(
    session: Session,
    *,
    owner_id: UUID,
    email: str,
    display_name: str,
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
                "complimentary_access": True,
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


async def _refresh_credentials_and_redeploy(
    *,
    service: Day30Mt5ConnectionService,
    session: Session,
    owner_id: UUID,
    user_id: UUID,
    password: str,
    server: str,
) -> Any:
    """Refresh the current MetaAPI terminal using the owner-submitted password.

    This avoids creating duplicate stale terminals when an already-provisioned live
    account is DEPLOYED but has not established a broker session. The password exists
    only for this request and is never persisted by Smart Signals.
    """
    row = session.execute(
        text(
            "SELECT id, metaapi_account_id FROM mt5_accounts WHERE owner_user_id=:user_id LIMIT 1"
        ),
        {"user_id": user_id},
    ).mappings().first()
    if row is None or not row["metaapi_account_id"]:
        raise Mt5ConnectionError("mt5_account_not_configured")

    token = service.resolve_platform_token()
    gateway = service._gateway  # noqa: SLF001 - canonical service owns this gateway
    remote_account_id = str(row["metaapi_account_id"])

    try:
        await gateway._request(  # noqa: SLF001 - supported MetaAPI account update API
            "PUT",
            f"/users/current/accounts/{remote_account_id}",
            token=token,
            json={
                "password": password,
                "name": "Smart Signals MT5",
                "server": server.strip(),
                "magic": SUPER_SIGNALS_MAGIC,
            },
            accepted_statuses={200, 204},
        )
        await gateway._request(  # noqa: SLF001 - supported MetaAPI redeploy API
            "POST",
            f"/users/current/accounts/{remote_account_id}/redeploy",
            token=token,
            accepted_statuses={200, 201, 202, 204},
        )

        deadline = time.monotonic() + 150
        remote = None
        while time.monotonic() < deadline:
            remote = await gateway.read_account(token=token, account_id=remote_account_id)
            if remote.state in {"DEPLOY_FAILED", "REDEPLOY_FAILED"}:
                raise MetaApiGatewayError("metaapi_deploy_failed")
            if remote.state == "DEPLOYED" and remote.connection_status == "CONNECTED":
                break
            await asyncio.sleep(2)

        if remote is None:
            raise MetaApiGatewayError("metaapi_timeout", retryable=True)

        service._write_remote_state(row["id"], remote)  # noqa: SLF001 - established auditable writer
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type="mt5.owner_onboarding_credential_refresh",
                entity_type="user",
                entity_id=user_id,
                payload={
                    "remote_state": remote.state,
                    "remote_connection_status": remote.connection_status,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()
    except MetaApiGatewayError as exc:
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type="mt5.owner_onboarding_credential_refresh_failed",
                entity_type="user",
                entity_id=user_id,
                payload={"error_code": exc.code, "trade_action_created": False},
            )
        )
        session.commit()
        raise Mt5ConnectionError(exc.code) from exc

    return service.get_user_status(user_id)


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
    )

    try:
        service.approve_user_account(
            approver_user_id=identity["id"],
            user_id=user_id,
            login=payload.mt5_login,
            server=payload.mt5_server,
        )
        password = payload.mt5_password.get_secret_value()
        view = await service.connect_user_live(
            user_id=user_id,
            login=payload.mt5_login,
            password=password,
            server=payload.mt5_server,
        )
        if not _is_connected(view):
            view = await _refresh_credentials_and_redeploy(
                service=service,
                session=session,
                owner_id=identity["id"],
                user_id=user_id,
                password=password,
                server=payload.mt5_server,
            )
    except Mt5ConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": exc.code,
                "message": "The member is safe but the live Vantage MT5 broker session was not verified. Trading remains stopped.",
            },
        ) from exc

    profiles = Mt5AccountProfileService(
        session_factory=get_session_factory(),
        connection_service=service,
    )
    profiles.sync_active_profile_from_canonical(user_id)
    ready = _is_connected(view)
    if ready:
        _activate_verified_member(session, owner_id=identity["id"], user_id=user_id)

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return OnboardLiveMemberResponse(
        user_id=user_id,
        email=email,
        display_name=display_name,
        created=created,
        complimentary_access=True,
        mt5_login_masked=view.login_masked,
        mt5_server=view.server,
        mt5_status=view.status,
        remote_state=view.remote_state,
        remote_connection_status=view.remote_connection_status,
        trading_status="active" if ready else "stopped",
        risk_percent=1.0,
        ready=ready,
        welcome_email_sent=False,
        welcome_email_reason="owner_test_pending" if ready else "mt5_not_connected",
    )
