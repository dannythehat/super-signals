"""Read-only Day 15 classification review endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session

router = APIRouter(
    prefix="/admin/telegram/classifications",
    tags=["telegram classifications"],
)
DbSession = Annotated[Session, Depends(get_db_session)]
SourceManager = Annotated[
    dict[str, Any],
    Depends(require_permission("sources.manage")),
]


class RecentClassificationResponse(BaseModel):
    message_id: UUID
    source_id: UUID
    source_title: str
    telegram_message_id: int
    revision_index: int
    classified_text: str
    classification: str
    decision_status: str
    reason: str
    matched_rules: list[str]
    classifier_version: str
    classified_at: datetime


@router.get("/recent", response_model=list[RecentClassificationResponse])
def list_recent_classifications(
    response: Response,
    session: DbSession,
    identity: SourceManager,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
) -> list[RecentClassificationResponse]:
    del identity
    rows = session.execute(
        text(
            """
            SELECT
                mc.message_id,
                m.source_id,
                COALESCE(s.chat_title, s.source_alias) AS source_title,
                m.telegram_message_id,
                mc.revision_index,
                CASE
                    WHEN mc.revision_index = 0 THEN m.raw_text
                    ELSE mr.raw_text
                END AS classified_text,
                mc.classification,
                mc.decision_status,
                mc.reason,
                mc.matched_rules,
                mc.classifier_version,
                mc.created_at AS classified_at
            FROM message_classifications AS mc
            JOIN messages AS m ON m.id = mc.message_id
            JOIN sources AS s ON s.id = m.source_id
            LEFT JOIN message_revisions AS mr
              ON mr.message_id = mc.message_id
             AND mr.revision_index = mc.revision_index
            ORDER BY mc.created_at DESC, mc.revision_index DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return [
        RecentClassificationResponse(
            message_id=row["message_id"],
            source_id=row["source_id"],
            source_title=str(row["source_title"]),
            telegram_message_id=int(row["telegram_message_id"]),
            revision_index=int(row["revision_index"]),
            classified_text=str(row["classified_text"] or ""),
            classification=str(row["classification"]),
            decision_status=str(row["decision_status"]),
            reason=str(row["reason"]),
            matched_rules=[str(item) for item in (row["matched_rules"] or [])],
            classifier_version=str(row["classifier_version"]),
            classified_at=row["classified_at"],
        )
        for row in rows
    ]
