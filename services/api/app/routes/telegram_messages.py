"""Read-only internal message log for Day 12 Telegram ingestion acceptance."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session
from app.models import Message, Source

router = APIRouter(prefix="/admin/telegram/messages", tags=["telegram messages"])
DbSession = Annotated[Session, Depends(get_db_session)]
SourceManager = Annotated[
    dict[str, Any],
    Depends(require_permission("sources.manage")),
]


class RecentTelegramMessageResponse(BaseModel):
    message_id: UUID
    source_id: UUID
    source_title: str
    telegram_message_id: int
    raw_text: str
    posted_at: datetime
    received_at: datetime
    ingestion_status: str


@router.get("/recent", response_model=list[RecentTelegramMessageResponse])
def list_recent_messages(
    response: Response,
    session: DbSession,
    identity: SourceManager,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RecentTelegramMessageResponse]:
    del identity
    rows = session.execute(
        select(Message, Source)
        .join(Source, Source.id == Message.source_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    ).all()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return [
        RecentTelegramMessageResponse(
            message_id=message.id,
            source_id=source.id,
            source_title=source.chat_title or source.source_alias,
            telegram_message_id=message.telegram_message_id,
            raw_text=message.raw_text,
            posted_at=message.posted_at,
            received_at=message.created_at,
            ingestion_status=message.ingestion_status,
        )
        for message, source in rows
    ]
