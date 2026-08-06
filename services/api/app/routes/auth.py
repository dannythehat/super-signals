"""Secure account authentication routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth_service import (
    authenticate_user,
    create_recovery_request,
    create_session,
    get_user_for_session,
    revoke_session,
)
from app.config import Settings, get_settings
from app.db import get_db_session
from app.permissions import ROLE_LABELS, build_access_sections

router = APIRouter(prefix="/auth", tags=["authentication"])
DbSession = Annotated[Session, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class RecoveryRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


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


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


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


@router.post("/login", response_model=AccountResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> AccountResponse:
    identity = authenticate_user(session, payload.email, payload.password)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect.",
        )

    raw_token, _ = create_session(
        session,
        user_id=identity["id"],
        ttl_seconds=settings.session_ttl_seconds,
        user_agent=request.headers.get("user-agent"),
        ip_address=_client_ip(request),
        fingerprint_secret=settings.session_fingerprint_secret,
    )
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return account_response(identity)


@router.get("/me", response_model=AccountResponse)
def me(
    request: Request,
    session: DbSession,
    settings: AppSettings,
) -> AccountResponse:
    identity = get_user_for_session(session, _session_token(request, settings))
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
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
