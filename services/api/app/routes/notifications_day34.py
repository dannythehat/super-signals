"""Day 34 authenticated in-app notifications.

Notifications are derived from canonical broker/lifecycle events and stored in
PostgreSQL. Reading or acknowledging a notification is UI state only and can never
place, close, modify or otherwise affect a trade.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.access_control import get_current_identity
from app.db import get_db_session

router = APIRouter(prefix="/notifications", tags=["notifications-day34"])
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]
DbSession = Annotated[Session, Depends(get_db_session)]


class NotificationResponse(BaseModel):
    id: UUID
    signal_id: UUID | None
    kind: str
    title: str
    body: str
    created_at: datetime
    read_at: datetime | None


class NotificationListResponse(BaseModel):
    notifications: tuple[NotificationResponse, ...]
    unread_count: int
    broker_trade_action_created: bool = False


class NotificationReadResponse(BaseModel):
    notification_id: UUID
    read_at: datetime
    already_read: bool
    broker_trade_action_created: bool = False


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _visible_filter() -> str:
    return "(n.audience = 'shared' OR (n.audience = 'user' AND n.user_id = :user_id))"


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    response: Response,
    session: DbSession,
    identity: Identity,
    unread_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
) -> NotificationListResponse:
    user_id = identity["id"]
    unread_clause = "AND nr.notification_id IS NULL" if unread_only else ""
    rows = session.execute(
        text(
            f"""
            SELECT
                n.id,
                n.signal_id,
                n.kind,
                n.title,
                n.body,
                n.created_at,
                nr.read_at
            FROM notification_events AS n
            LEFT JOIN notification_reads AS nr
              ON nr.notification_id = n.id
             AND nr.user_id = :user_id
            WHERE {_visible_filter()}
              {unread_clause}
            ORDER BY n.created_at DESC, n.id DESC
            LIMIT :limit
            """
        ),
        {"user_id": user_id, "limit": limit},
    ).mappings().all()
    unread_count = int(
        session.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM notification_events AS n
                LEFT JOIN notification_reads AS nr
                  ON nr.notification_id = n.id
                 AND nr.user_id = :user_id
                WHERE {_visible_filter()}
                  AND nr.notification_id IS NULL
                """
            ),
            {"user_id": user_id},
        ).scalar_one()
    )
    _no_store(response)
    return NotificationListResponse(
        notifications=tuple(
            NotificationResponse(
                id=row["id"],
                signal_id=row["signal_id"],
                kind=str(row["kind"]),
                title=str(row["title"]),
                body=str(row["body"]),
                created_at=row["created_at"],
                read_at=row["read_at"],
            )
            for row in rows
        ),
        unread_count=unread_count,
    )


@router.post("/{notification_id}/read", response_model=NotificationReadResponse)
def mark_notification_read(
    notification_id: UUID,
    response: Response,
    session: DbSession,
    identity: Identity,
) -> NotificationReadResponse:
    user_id = identity["id"]
    visible = session.execute(
        text(
            f"""
            SELECT n.id
            FROM notification_events AS n
            WHERE n.id = :notification_id
              AND {_visible_filter()}
            LIMIT 1
            """
        ),
        {"notification_id": notification_id, "user_id": user_id},
    ).scalar_one_or_none()
    if visible is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "notification_not_found",
                "message": "Notification was not found.",
            },
        )

    existing = session.execute(
        text(
            """
            SELECT read_at
            FROM notification_reads
            WHERE notification_id = :notification_id
              AND user_id = :user_id
            """
        ),
        {"notification_id": notification_id, "user_id": user_id},
    ).scalar_one_or_none()
    already_read = existing is not None
    if existing is None:
        existing = session.execute(
            text(
                """
                INSERT INTO notification_reads (notification_id, user_id)
                VALUES (:notification_id, :user_id)
                ON CONFLICT (notification_id, user_id) DO UPDATE
                    SET read_at = notification_reads.read_at
                RETURNING read_at
                """
            ),
            {"notification_id": notification_id, "user_id": user_id},
        ).scalar_one()
        session.commit()

    _no_store(response)
    return NotificationReadResponse(
        notification_id=notification_id,
        read_at=existing,
        already_read=already_read,
    )
