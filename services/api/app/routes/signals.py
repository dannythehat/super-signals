"""Read-only Day 18 canonical Signal endpoint for administrators."""

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

router = APIRouter(prefix="/admin/signals", tags=["signals"])
DbSession = Annotated[Session, Depends(get_db_session)]
SourceManager = Annotated[dict[str, Any], Depends(require_permission("sources.manage"))]


class RecentSignalResponse(BaseModel):
    id: UUID
    source_id: UUID
    source_title: str
    provider_message_id: int
    source_revision_index: int
    source_posted_at: datetime
    signal_fingerprint: str
    symbol: str
    side: str
    order_type: str
    entry_price: Decimal
    stop_loss: Decimal
    take_profits: list[str]
    risk_multiplier: Decimal
    original_text: str
    created_at: datetime
    observation_count: int


@router.get("/recent", response_model=list[RecentSignalResponse])
def list_recent_signals(
    response: Response,
    session: DbSession,
    identity: SourceManager,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
) -> list[RecentSignalResponse]:
    del identity
    rows = session.execute(
        text(
            """
            SELECT
                sig.id,
                sig.source_id,
                COALESCE(s.chat_title, s.source_alias) AS source_title,
                sig.provider_message_id,
                sig.source_revision_index,
                sig.source_posted_at,
                sig.signal_fingerprint,
                sig.symbol,
                sig.side,
                sig.order_type,
                sig.entry_low AS entry_price,
                sig.stop_loss,
                sig.take_profits,
                sig.risk_multiplier,
                sig.original_text,
                sig.created_at,
                COUNT(obs.id) AS observation_count
            FROM signals AS sig
            JOIN sources AS s ON s.id = sig.source_id
            LEFT JOIN signal_observations AS obs ON obs.signal_id = sig.id
            GROUP BY sig.id, s.chat_title, s.source_alias
            ORDER BY sig.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return [
        RecentSignalResponse(
            id=row["id"],
            source_id=row["source_id"],
            source_title=str(row["source_title"]),
            provider_message_id=int(row["provider_message_id"]),
            source_revision_index=int(row["source_revision_index"]),
            source_posted_at=row["source_posted_at"],
            signal_fingerprint=str(row["signal_fingerprint"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            order_type=str(row["order_type"]),
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            take_profits=[str(item) for item in (row["take_profits"] or [])],
            risk_multiplier=row["risk_multiplier"],
            original_text=str(row["original_text"] or ""),
            created_at=row["created_at"],
            observation_count=int(row["observation_count"]),
        )
        for row in rows
    ]
