"""Read-only Day 17 admin review queue endpoint."""

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

router = APIRouter(prefix="/admin/telegram/reviews", tags=["telegram reviews"])
DbSession = Annotated[Session, Depends(get_db_session)]
SourceManager = Annotated[
    dict[str, Any],
    Depends(require_permission("sources.manage")),
]


class ReviewItemResponse(BaseModel):
    id: UUID
    message_id: UUID
    source_id: UUID
    source_title: str
    telegram_message_id: int
    revision_index: int
    review_stage: str
    review_status: str
    reason: str
    matched_rules: list[str]
    raw_text: str
    classification: str | None
    decision_status: str | None
    classifier_version: str | None
    parse_status: str | None
    parser_version: str | None
    validator_version: str | None
    symbol: str | None
    direction: str | None
    entry_price: Decimal | None
    stop_loss: Decimal | None
    take_profits: list[str]
    size_multiplier: Decimal | None
    queued_at: datetime


@router.get("/recent", response_model=list[ReviewItemResponse])
def list_recent_reviews(
    response: Response,
    session: DbSession,
    identity: SourceManager,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ReviewItemResponse]:
    del identity
    rows = session.execute(
        text(
            """
            SELECT
                ri.id,
                ri.message_id,
                m.source_id,
                COALESCE(s.chat_title, s.source_alias) AS source_title,
                m.telegram_message_id,
                ri.revision_index,
                ri.review_stage,
                ri.review_status,
                ri.reason,
                ri.matched_rules,
                ri.raw_text,
                ri.classification,
                ri.decision_status,
                ri.classifier_version,
                ri.parse_status,
                ri.parser_version,
                ri.validator_version,
                ri.symbol,
                ri.direction,
                ri.entry_price,
                ri.stop_loss,
                ri.take_profits,
                ri.size_multiplier,
                ri.created_at AS queued_at
            FROM message_review_items AS ri
            JOIN messages AS m ON m.id = ri.message_id
            JOIN sources AS s ON s.id = m.source_id
            ORDER BY ri.created_at DESC, ri.revision_index DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return [
        ReviewItemResponse(
            id=row["id"],
            message_id=row["message_id"],
            source_id=row["source_id"],
            source_title=str(row["source_title"]),
            telegram_message_id=int(row["telegram_message_id"]),
            revision_index=int(row["revision_index"]),
            review_stage=str(row["review_stage"]),
            review_status=str(row["review_status"]),
            reason=str(row["reason"]),
            matched_rules=[str(item) for item in (row["matched_rules"] or [])],
            raw_text=str(row["raw_text"] or ""),
            classification=(str(row["classification"]) if row["classification"] is not None else None),
            decision_status=(str(row["decision_status"]) if row["decision_status"] is not None else None),
            classifier_version=(str(row["classifier_version"]) if row["classifier_version"] is not None else None),
            parse_status=(str(row["parse_status"]) if row["parse_status"] is not None else None),
            parser_version=(str(row["parser_version"]) if row["parser_version"] is not None else None),
            validator_version=(str(row["validator_version"]) if row["validator_version"] is not None else None),
            symbol=(str(row["symbol"]) if row["symbol"] is not None else None),
            direction=(str(row["direction"]) if row["direction"] is not None else None),
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            take_profits=[str(item) for item in (row["take_profits"] or [])],
            size_multiplier=row["size_multiplier"],
            queued_at=row["queued_at"],
        )
        for row in rows
    ]
