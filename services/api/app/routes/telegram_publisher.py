"""Administrator visibility for the canonical Super Signals Telegram publisher."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session
from app.telegram_publisher_canonical import CanonicalTelegramPublisherManager

router = APIRouter(prefix="/admin/telegram-publisher", tags=["telegram-publisher"])
DbSession = Annotated[Session, Depends(get_db_session)]
SourceManager = Annotated[dict[str, Any], Depends(require_permission("sources.manage"))]


class PublisherStatusResponse(BaseModel):
    configured: bool
    enabled: bool
    destination_chat_type: str | None
    bot_membership_status: str | None
    minimum_permissions_ok: bool
    source_collision: bool
    reason: str


class PublisherTestResponse(BaseModel):
    status: str
    reason: str
    telegram_message_id: int | None


class PublicationResponse(BaseModel):
    id: UUID
    signal_id: UUID
    publication_kind: str
    status: str
    rendered_text: str | None
    telegram_message_id: int | None
    attempt_count: int
    failure_code: str | None
    failure_reason: str | None
    attempted_at: datetime | None
    sent_at: datetime | None
    created_at: datetime
    symbol: str
    side: str


def _manager(request: Request) -> CanonicalTelegramPublisherManager:
    return request.app.state.telegram_publisher


@router.get("/status", response_model=PublisherStatusResponse)
async def publisher_status(
    request: Request,
    response: Response,
    identity: SourceManager,
) -> PublisherStatusResponse:
    del identity
    manager = _manager(request)
    result = await asyncio.to_thread(manager.check_connection)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return PublisherStatusResponse(**asdict(result))


@router.post("/test-send", response_model=PublisherTestResponse)
async def publisher_test_send(
    request: Request,
    response: Response,
    identity: SourceManager,
) -> PublisherTestResponse:
    del identity
    manager = _manager(request)
    result = await asyncio.to_thread(manager.send_connection_test)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return PublisherTestResponse(**result)


@router.get("/publications/recent", response_model=list[PublicationResponse])
def recent_publications(
    response: Response,
    session: DbSession,
    identity: SourceManager,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
) -> list[PublicationResponse]:
    del identity
    rows = session.execute(
        text(
            """
            SELECT
                pub.id,
                pub.signal_id,
                pub.publication_kind,
                pub.status,
                pub.rendered_text,
                pub.telegram_message_id,
                pub.attempt_count,
                pub.failure_code,
                pub.failure_reason,
                pub.attempted_at,
                pub.sent_at,
                pub.created_at,
                sig.symbol,
                sig.side
            FROM telegram_publications AS pub
            JOIN signals AS sig ON sig.id = pub.signal_id
            ORDER BY pub.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return [PublicationResponse(**dict(row)) for row in rows]
