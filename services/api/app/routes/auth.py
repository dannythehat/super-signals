"""Secure account authentication routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth_rate_limit_day39 import (
    ADMIN_SETUP_POLICY,
    LOGIN_POLICY,
    RECOVERY_POLICY,
    RateLimitExceeded,
    assert_not_limited,
    clear_failures,
    record_attempt,
    record_failure,
)
from app.auth_service import (
    authenticate_user,
    create_recovery_request,
    create_session,
    get_user_for_session,
    revoke_session,
)
from app.config import Settings, get_settings
from app.db import get_db_session
from app.models import AuditEvent
from app.permissions import ROLE_LABELS, build_access_sections
from app.security import hash_password, hash_token

router = APIRouter(prefix="/auth", tags=["authentication"])
DbSession = Annotated[Session, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class RecoveryRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class AdminSetupRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    password: str = Field(min_length=12, max_length=128)


class SecurityStatus(BaseModel):
    two_factor: str
    passkey: str


class AccessAction(BaseModel):
    permission: str
    label: str
    description: str


class AccessSection(BaseModel):
    key: str
    label: str
    description: str
    actions: list[AccessAction]


class AccountResponse(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str
    role_label: str
    roles: list[str]
    permissions: list[str]
    sections: list[AccessSection]
    security: SecurityStatus


class RecoveryResponse(BaseModel):
    message: str


class AdminSetupResponse(BaseModel):
    message: str
    email: str


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _too_many_attempts() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many attempts. Try again later.",
        headers={"Retry-After": "900"},
    )


def account_response(identity: dict[str, Any]) -> AccountResponse:
    return AccountResponse(
        id=str(identity["id"]),
        email=str(identity["email"]),
        display_name=identity["display_name"],
        role=identity["role"],
        role_label=ROLE_LABELS.get(identity["role"], identity["role"]),
        roles=list(identity["roles"]),
        permissions=list(identity["permissions"]),
        sections=build_access_sections(identity["permissions"]),
        security=SecurityStatus(
            two_factor="enabled" if identity["two_factor_enabled"] else "setup_required",
            passkey="enabled" if identity["passkey_enabled"] else "setup_available",
        ),
    )


def _session_token(request: Request, settings: Settings) -> str:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return token


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )


@router.post("/login", response_model=AccountResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> AccountResponse:
    try:
        assert_not_limited(
            session,
            policy=LOGIN_POLICY,
            subject=payload.email,
            fingerprint_secret=settings.session_fingerprint_secret,
        )
    except RateLimitExceeded as exc:
        raise _too_many_attempts() from exc

    identity = authenticate_user(session, payload.email, payload.password)
    if identity is None:
        record_failure(
            session,
            policy=LOGIN_POLICY,
            subject=payload.email,
            fingerprint_secret=settings.session_fingerprint_secret,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect.",
        )

    clear_failures(
        session,
        policy=LOGIN_POLICY,
        subject=payload.email,
        fingerprint_secret=settings.session_fingerprint_secret,
    )
    raw_token, _ = create_session(
        session,
        user_id=identity["id"],
        ttl_seconds=settings.session_ttl_seconds,
        user_agent=request.headers.get("user-agent"),
        ip_address=_client_ip(request),
        fingerprint_secret=settings.session_fingerprint_secret,
    )
    _set_session_cookie(response, settings, raw_token)
    response.headers["Cache-Control"] = "no-store"
    return account_response(identity)


@router.post("/admin-setup", response_model=AdminSetupResponse)
def complete_admin_setup(
    payload: AdminSetupRequest,
    session: DbSession,
    settings: AppSettings,
) -> AdminSetupResponse:
    try:
        assert_not_limited(
            session,
            policy=ADMIN_SETUP_POLICY,
            subject=payload.token,
            fingerprint_secret=settings.session_fingerprint_secret,
        )
    except RateLimitExceeded as exc:
        raise _too_many_attempts() from exc

    # Trading Admin setup tokens are deliberately single-use but do not expire
    # with time. They remain valid until completed or explicitly replaced by an
    # Owner, at which point the previous unused token is marked used. Ordinary
    # password-recovery tokens retain their normal expiry behaviour elsewhere.
    setup = (
        session.execute(
            text(
                """
                SELECT pr.id, pr.user_id, u.email
                FROM password_recovery_requests AS pr
                JOIN users AS u ON u.id = pr.user_id
                WHERE pr.token_hash = :token_hash
                  AND pr.used_at IS NULL
                  AND u.status IN ('invited', 'active')
                  AND EXISTS (
                      SELECT 1
                      FROM user_roles AS ur
                      JOIN roles AS r ON r.id = ur.role_id
                      WHERE ur.user_id = u.id
                        AND r.name = 'trading_admin'
                  )
                ORDER BY pr.created_at DESC
                LIMIT 1
                """
            ),
            {"token_hash": hash_token(payload.token)},
        )
        .mappings()
        .first()
    )
    if setup is None:
        record_failure(
            session,
            policy=ADMIN_SETUP_POLICY,
            subject=payload.token,
            fingerprint_secret=settings.session_fingerprint_secret,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This administrator setup link is invalid, already used, or has been replaced.",
        )

    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    session.execute(
        text(
            """
            UPDATE users
            SET password_hash = :password_hash,
                status = 'active',
                updated_at = now()
            WHERE id = :user_id
            """
        ),
        {"password_hash": password_hash, "user_id": setup["user_id"]},
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
        {"user_id": setup["user_id"]},
    )
    session.add(
        AuditEvent(
            actor_user_id=setup["user_id"],
            event_type="admin.trading_admin_setup_completed",
            entity_type="user",
            entity_id=setup["user_id"],
            payload={"email": str(setup["email"]), "role": "trading_admin"},
        )
    )
    session.commit()
    clear_failures(
        session,
        policy=ADMIN_SETUP_POLICY,
        subject=payload.token,
        fingerprint_secret=settings.session_fingerprint_secret,
    )

    return AdminSetupResponse(
        message="Trading Admin account is ready. You can sign in now.",
        email=str(setup["email"]),
    )


@router.get("/me", response_model=AccountResponse)
def me(
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> AccountResponse:
    raw_token = _session_token(request, settings)
    identity = get_user_for_session(
        session,
        raw_token,
        user_agent=request.headers.get("user-agent"),
        fingerprint_secret=settings.session_fingerprint_secret,
    )
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    _set_session_cookie(response, settings, raw_token)
    response.headers["Cache-Control"] = "no-store"
    return account_response(identity)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> Response:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        revoke_session(session, token)
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(
    "/recovery",
    response_model=RecoveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def recovery(
    payload: RecoveryRequest,
    request: Request,
    session: DbSession,
    settings: AppSettings,
) -> RecoveryResponse:
    try:
        assert_not_limited(
            session,
            policy=RECOVERY_POLICY,
            subject=payload.email,
            fingerprint_secret=settings.session_fingerprint_secret,
        )
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many recovery requests. Try again later.",
            headers={"Retry-After": "3600"},
        ) from exc

    record_attempt(
        session,
        policy=RECOVERY_POLICY,
        subject=payload.email,
        fingerprint_secret=settings.session_fingerprint_secret,
    )
    create_recovery_request(
        session,
        email=payload.email,
        ttl_seconds=settings.recovery_ttl_seconds,
        ip_address=_client_ip(request),
        fingerprint_secret=settings.session_fingerprint_secret,
    )
    return RecoveryResponse(
        message="If the account is eligible, recovery instructions will be sent."
    )
