"""Read-only Day 16 parser review endpoint."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session

router = APIRouter(prefix="/admin/telegram/parses", tags=["telegram parses"])
DbSession = Annotated[Session, Depends(get_db_session)]
SourceManager = Annotated[
    dict[str, Any],
    Depends(require_permission("sources.manage")),
]


class RecentParseResponse(BaseModel):
    message_id: UUID
    source_id: UUID
    source_title: str
    telegram_message_id: int
    revision_index: int
    parsed_text: str
    parse_status: str
    reason: str
    matched_rules: list[str]
    parser_version: str
    symbol: str | None
    direction: str | None
    entry_price: Decimal | None
    stop_loss: Decimal | None
    take_profits: list[str]
    size_multiplier: Decimal | None
    parsed_at: datetime


@router.get("/recent", response_model=list[RecentParseResponse])
def list_recent_parses(
    response: Response,
    session: DbSession,
    identity: SourceManager,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
) -> list[RecentParseResponse]:
    del identity
    rows = session.execute(
        text(
            """
            SELECT
                mp.message_id,
                m.source_id,
                COALESCE(s.chat_title, s.source_alias) AS source_title,
                m.telegram_message_id,
                mp.revision_index,
                CASE
                    WHEN mp.revision_index = 0 THEN m.raw_text
                    ELSE mr.raw_text
                END AS parsed_text,
                mp.parse_status,
                mp.reason,
                mp.matched_rules,
                mp.parser_version,
                mp.symbol,
                mp.direction,
                mp.entry_price,
                mp.stop_loss,
                mp.take_profits,
                mp.size_multiplier,
                mp.created_at AS parsed_at
            FROM message_parses AS mp
            JOIN messages AS m ON m.id = mp.message_id
            JOIN sources AS s ON s.id = m.source_id
            LEFT JOIN message_revisions AS mr
              ON mr.message_id = mp.message_id
             AND mr.revision_index = mp.revision_index
            ORDER BY mp.created_at DESC, mp.revision_index DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return [
        RecentParseResponse(
            message_id=row["message_id"],
            source_id=row["source_id"],
            source_title=str(row["source_title"]),
            telegram_message_id=int(row["telegram_message_id"]),
            revision_index=int(row["revision_index"]),
            parsed_text=str(row["parsed_text"] or ""),
            parse_status=str(row["parse_status"]),
            reason=str(row["reason"]),
            matched_rules=[str(item) for item in (row["matched_rules"] or [])],
            parser_version=str(row["parser_version"]),
            symbol=str(row["symbol"]) if row["symbol"] is not None else None,
            direction=str(row["direction"]) if row["direction"] is not None else None,
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            take_profits=[str(item) for item in (row["take_profits"] or [])],
            size_multiplier=row["size_multiplier"],
            parsed_at=row["parsed_at"],
        )
        for row in rows
    ]
