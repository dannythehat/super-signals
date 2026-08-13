"""Authentication dependencies and audited permission enforcement."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth_service import get_user_for_session
from app.config import Settings, get_settings
from app.db import get_db_session
from app.models import AuditEvent

DbSession = Annotated[Session, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]

DENIAL_MESSAGE = "You do not have permission to perform this action."


def _session_token(request: Request, settings: Settings) -> str:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return token


def _identity_for_request(
    request: Request,
    session: Session,
    settings: Settings,
) -> dict[str, Any] | None:
    return get_user_for_session(
        session,
        _session_token(request, settings),
        user_agent=request.headers.get("user-agent"),
        fingerprint_secret=settings.session_fingerprint_secret,
    )


def get_current_identity(
    request: Request,
    session: DbSession,
    settings: AppSettings,
) -> dict[str, Any]:
    identity = _identity_for_request(request, session, settings)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return identity


def _request_id(request: Request) -> UUID:
    raw = request.headers.get("x-request-id")
    if raw:
        try:
            return UUID(raw)
        except ValueError:
            pass
    return uuid4()


def audit_permission_denial(
    session: Session,
    *,
    identity: dict[str, Any],
    permission: str,
    request: Request,
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=identity["id"],
            event_type="permission.denied",
            entity_type="permission",
            payload={
                "permission": permission,
                "role": identity["role"],
                "method": request.method,
                "path": request.url.path,
            },
            request_id=_request_id(request),
        )
    )
    session.commit()


def require_permission(permission: str):
    def check_permission(
        request: Request,
        session: DbSession,
        settings: AppSettings,
    ) -> dict[str, Any]:
        identity = _identity_for_request(request, session, settings)
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required.",
            )
        if permission not in identity["permissions"]:
            audit_permission_denial(
                session,
                identity=identity,
                permission=permission,
                request=request,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "permission_denied",
                    "message": DENIAL_MESSAGE,
                    "permission": permission,
                },
            )
        return identity

    return check_permission
