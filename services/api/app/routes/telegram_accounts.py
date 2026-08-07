"""Authenticated administrator routes for Telegram account connections."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, SecretStr

from app.access_control import DbSession, require_permission
from app.telegram_crypto import SessionDecryptionError
from app.telegram_gateway import (
    TelegramFlowNotFoundError,
    TelegramGatewayError,
    TelegramPasswordInvalidError,
    TelegramSessionInvalidError,
)
from app.telegram_service import (
    TelegramConfigurationError,
    TelegramConnectionConflictError,
    TelegramConnectionNotFoundError,
    TelegramConnectionService,
    TelegramConnectionView,
    get_telegram_connection_service,
)

router = APIRouter(prefix="/admin/telegram/accounts", tags=["telegram-accounts"])
AdminIdentity = Annotated[
    dict[str, Any],
    Depends(require_permission("sources.manage")),
]


class BeginTelegramAuthorizationRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class TelegramPasswordRequest(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=256)


class TelegramAccountResponse(BaseModel):
    id: UUID
    label: str
    phone_hint: str
    status: str
    last_connected_at: datetime | None


class TelegramAuthorizationStartResponse(BaseModel):
    flow_id: UUID
    status: Literal["pending"] = "pending"
    qr_url: str
    expires_at: datetime


class TelegramAuthorizationPollResponse(BaseModel):
    flow_id: UUID
    status: Literal["pending", "password_required", "connected", "expired"]
    account: TelegramAccountResponse | None = None


class TelegramDisconnectResponse(BaseModel):
    disconnected: bool
    server_session_destroyed: bool
    remote_logout: bool


def provide_telegram_connection_service() -> TelegramConnectionService:
    try:
        return get_telegram_connection_service()
    except TelegramConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "telegram_not_configured",
                "message": "Telegram connection is not configured on this server.",
            },
        ) from exc


TelegramService = Annotated[
    TelegramConnectionService,
    Depends(provide_telegram_connection_service),
]


def _account_response(account: TelegramConnectionView) -> TelegramAccountResponse:
    return TelegramAccountResponse(
        id=account.id,
        label=account.label,
        phone_hint=account.phone_hint,
        status=account.status,
        last_connected_at=account.last_connected_at,
    )


def _translate_telegram_error(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (
            TelegramConnectionNotFoundError,
            TelegramFlowNotFoundError,
        ),
    ):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "telegram_connection_not_found",
                "message": "Telegram connection was not found.",
            },
        )
    if isinstance(exc, TelegramConnectionConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "telegram_connection_conflict",
                "message": str(exc),
            },
        )
    if isinstance(exc, TelegramPasswordInvalidError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "telegram_password_invalid",
                "message": "Telegram rejected the two-step verification password.",
            },
        )
    if isinstance(exc, (SessionDecryptionError, TelegramSessionInvalidError)):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "telegram_session_invalid",
                "message": "The saved Telegram session is no longer valid.",
            },
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_request", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": "telegram_unavailable",
            "message": "Telegram could not complete the request.",
        },
    )


@router.get("", response_model=list[TelegramAccountResponse])
def list_telegram_accounts(
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramService,
) -> list[TelegramAccountResponse]:
    return [
        _account_response(account)
        for account in service.list_accounts(session, actor=identity)
    ]


@router.post(
    "/authorize",
    response_model=TelegramAuthorizationStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def begin_telegram_authorization(
    body: BeginTelegramAuthorizationRequest,
    response: Response,
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramService,
) -> TelegramAuthorizationStartResponse:
    try:
        authorization = await service.begin_authorization(
            session,
            actor=identity,
            label=body.label,
        )
    except Exception as exc:
        raise _translate_telegram_error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TelegramAuthorizationStartResponse(
        flow_id=authorization.flow_id,
        qr_url=authorization.qr_url,
        expires_at=authorization.expires_at,
    )


@router.get(
    "/authorize/{flow_id}",
    response_model=TelegramAuthorizationPollResponse,
)
async def poll_telegram_authorization(
    flow_id: UUID,
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramService,
) -> TelegramAuthorizationPollResponse:
    try:
        result = await service.poll_authorization(
            session,
            actor=identity,
            flow_id=flow_id,
        )
    except Exception as exc:
        raise _translate_telegram_error(exc) from exc

    account = result.get("account")
    return TelegramAuthorizationPollResponse(
        flow_id=flow_id,
        status=result["status"],
        account=_account_response(account) if account is not None else None,
    )


@router.post(
    "/authorize/{flow_id}/password",
    response_model=TelegramAuthorizationPollResponse,
)
async def submit_telegram_password(
    flow_id: UUID,
    body: TelegramPasswordRequest,
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramService,
) -> TelegramAuthorizationPollResponse:
    try:
        result = await service.submit_password(
            session,
            actor=identity,
            flow_id=flow_id,
            password=body.password.get_secret_value(),
        )
    except Exception as exc:
        raise _translate_telegram_error(exc) from exc

    account = result.get("account")
    return TelegramAuthorizationPollResponse(
        flow_id=flow_id,
        status=result["status"],
        account=_account_response(account) if account is not None else None,
    )


@router.post(
    "/{account_id}/verify",
    response_model=TelegramAccountResponse,
)
async def verify_telegram_account(
    account_id: UUID,
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramService,
) -> TelegramAccountResponse:
    try:
        account = await service.verify_account(
            session,
            actor=identity,
            account_id=account_id,
        )
    except Exception as exc:
        raise _translate_telegram_error(exc) from exc
    return _account_response(account)


@router.post(
    "/{account_id}/disconnect",
    response_model=TelegramDisconnectResponse,
)
async def disconnect_telegram_account(
    account_id: UUID,
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramService,
) -> TelegramDisconnectResponse:
    try:
        result = await service.disconnect_account(
            session,
            actor=identity,
            account_id=account_id,
        )
    except Exception as exc:
        raise _translate_telegram_error(exc) from exc
    return TelegramDisconnectResponse(**result)
