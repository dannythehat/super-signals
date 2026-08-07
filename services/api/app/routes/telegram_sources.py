"""Administrator-only Telegram group/channel discovery and explicit source selection."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.access_control import DbSession, require_permission
from app.telegram_crypto import SessionDecryptionError
from app.telegram_gateway import TelegramSessionInvalidError
from app.telegram_source_gateway import TelegramSourceGatewayError
from app.telegram_source_service import (
    TelegramSelectableSourceView,
    TelegramSourceConfigurationError,
    TelegramSourceNotFoundError,
    TelegramSourceService,
    get_telegram_source_service,
)

router = APIRouter(prefix="/admin/telegram/sources", tags=["telegram-sources"])
AdminIdentity = Annotated[
    dict[str, Any],
    Depends(require_permission("sources.manage")),
]


class TelegramSourceSelectionRequest(BaseModel):
    chat_id: int


class TelegramSelectableSourceResponse(BaseModel):
    chat_id: int
    title: str
    kind: Literal["group", "channel"]
    selected: bool
    source_id: UUID | None
    status: str | None


class TelegramSourceRemovalResponse(BaseModel):
    removed: bool
    source_id: UUID
    monitoring_started: bool


def provide_telegram_source_service() -> TelegramSourceService:
    try:
        return get_telegram_source_service()
    except TelegramSourceConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "telegram_not_configured",
                "message": "Telegram source discovery is not configured on this server.",
            },
        ) from exc


TelegramSources = Annotated[
    TelegramSourceService,
    Depends(provide_telegram_source_service),
]


def _response(item: TelegramSelectableSourceView) -> TelegramSelectableSourceResponse:
    return TelegramSelectableSourceResponse(
        chat_id=item.chat_id,
        title=item.title,
        kind=item.kind,
        selected=item.selected,
        source_id=item.source_id,
        status=item.status,
    )


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TelegramSourceNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "telegram_source_not_found", "message": str(exc)},
        )
    if isinstance(exc, (SessionDecryptionError, TelegramSessionInvalidError)):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "telegram_session_invalid",
                "message": (
                    "The saved Telegram reader session is not available for source selection."
                ),
            },
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_request", "message": str(exc)},
        )
    if isinstance(exc, TelegramSourceGatewayError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "telegram_unavailable",
                "message": "Telegram groups and channels could not be listed.",
            },
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": "telegram_unavailable",
            "message": "Telegram could not complete the source-selection request.",
        },
    )


@router.get(
    "/accounts/{account_id}/available",
    response_model=list[TelegramSelectableSourceResponse],
)
async def list_available_sources(
    account_id: UUID,
    response: Response,
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramSources,
) -> list[TelegramSelectableSourceResponse]:
    try:
        items = await service.discover_sources(
            session,
            actor=identity,
            account_id=account_id,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return [_response(item) for item in items]


@router.post(
    "/accounts/{account_id}/select",
    response_model=TelegramSelectableSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def select_source(
    account_id: UUID,
    body: TelegramSourceSelectionRequest,
    response: Response,
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramSources,
) -> TelegramSelectableSourceResponse:
    try:
        item = await service.select_source(
            session,
            actor=identity,
            account_id=account_id,
            chat_id=body.chat_id,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return _response(item)


@router.post(
    "/accounts/{account_id}/selected/{source_id}/remove",
    response_model=TelegramSourceRemovalResponse,
)
def remove_selected_source(
    account_id: UUID,
    source_id: UUID,
    response: Response,
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramSources,
) -> TelegramSourceRemovalResponse:
    try:
        result = service.unselect_source(
            session,
            actor=identity,
            account_id=account_id,
            source_id=source_id,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TelegramSourceRemovalResponse(**result)
