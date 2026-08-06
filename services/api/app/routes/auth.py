"""Secure owner authentication routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth_service import (
    authenticate_owner,
    create_owner_session,
    create_recovery_request,
    get_owner_for_session,
    revoke_session,
)
from app.config import Settings, get_settings
from app.db import get_db_session

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


class OwnerResponse(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str = "owner"
    security: SecurityStatus


class RecoveryResponse(BaseModel):
    message: str


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _owner_response(owner: dict[str, Any]) -> OwnerResponse:
    return OwnerResponse(
        id=str(owner["id"]),
        email=str(owner["email"]),
        display_name=owner["display_name"],
        security=SecurityStatus(
            two_factor="enabled" if owner["two_factor_enabled"] else "setup_required",
            passkey="enabled" if owner["passkey_enabled"] else "setup_available",
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


@router.post("/login", response_model=OwnerResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> OwnerResponse:
    owner = authenticate_owner(session, payload.email, payload.password)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect.",
        )

    raw_token, _ = create_owner_session(
        session,
        user_id=owner["id"],
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
    return _owner_response(owner)


@router.get("/me", response_model=OwnerResponse)
def me(
    request: Request,
    session: DbSession,
    settings: AppSettings,
) -> OwnerResponse:
    owner = get_owner_for_session(session, _session_token(request, settings))
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return _owner_response(owner)


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
