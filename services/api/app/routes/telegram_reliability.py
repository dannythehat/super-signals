"""Day 14 administrator visibility for Telegram source connectivity."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.access_control import DbSession, require_permission
from app.telegram_source_service_day14 import (
    Day14TelegramSourceService,
    get_day14_telegram_source_service,
)
from app.telegram_source_service import TelegramSourceConfigurationError

router = APIRouter(prefix="/admin/telegram/reliability", tags=["telegram-reliability"])
AdminIdentity = Annotated[
    dict[str, Any],
    Depends(require_permission("sources.manage")),
]


class TelegramSourceReliabilityResponse(BaseModel):
    source_id: UUID
    chat_id: int
    title: str
    status: str
    connection_status: Literal["connected", "disconnected"]
    connected_reader_count: int


def provide_day14_telegram_source_service() -> Day14TelegramSourceService:
    try:
        return get_day14_telegram_source_service()
    except TelegramSourceConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "telegram_not_configured",
                "message": "Telegram reliability checks are not configured on this server.",
            },
        ) from exc


TelegramReliabilityService = Annotated[
    Day14TelegramSourceService,
    Depends(provide_day14_telegram_source_service),
]


@router.get("/sources", response_model=list[TelegramSourceReliabilityResponse])
def list_source_reliability(
    response: Response,
    session: DbSession,
    identity: AdminIdentity,
    service: TelegramReliabilityService,
) -> list[TelegramSourceReliabilityResponse]:
    del identity
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return [
        TelegramSourceReliabilityResponse(
            source_id=item.source_id,
            chat_id=item.chat_id,
            title=item.title,
            status=item.status,
            connection_status=item.connection_status,
            connected_reader_count=item.connected_reader_count,
        )
        for item in service.list_shared_sources(session)
    ]
