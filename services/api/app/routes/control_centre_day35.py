"""Day 35 Owner/Trading Admin control-centre endpoint."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.control_centre_day35 import Day35ControlCentreService
from app.db import get_db_session, get_session_factory

router = APIRouter(prefix="/admin/day35", tags=["day35-admin"])
ActivityAdmin = Annotated[dict[str, Any], Depends(require_permission("activity.view"))]
DbSession = Annotated[Session, Depends(get_db_session)]


class ReviewStageResponse(BaseModel):
    stage: str
    open_count: int
    oldest_at: datetime | None
    newest_at: datetime | None


class AttentionResponse(BaseModel):
    key: str
    tone: str
    title: str
    detail: str
    count: int
    area: str


class RecentEventResponse(BaseModel):
    event_type: str
    entity_type: str
    created_at: datetime


class ControlCentreResponse(BaseModel):
    generated_at: datetime
    overall_status: str
    open_signals: int
    pending_signals: int
    trading_active_users: int
    trading_stopped_users: int
    source_live: int
    source_testing: int
    source_paused: int
    source_revoked: int
    telegram_connected: int
    telegram_attention: int
    mt5_connected: int
    mt5_attention: int
    active_users: int
    invited_users: int
    suspended_users: int
    revoked_users: int
    review_open: int
    review_open_24h: int
    message_errors_24h: int
    publication_failed: int
    publication_pending: int
    push_failed_24h: int
    telegram_notification_failed_24h: int
    live_board_ready: bool
    live_board_pinned: bool
    review_stages: tuple[ReviewStageResponse, ...]
    attention: tuple[AttentionResponse, ...]
    recent_events: tuple[RecentEventResponse, ...]
    broker_trade_action_created: bool


@router.get("/control-centre", response_model=ControlCentreResponse)
def control_centre(
    response: Response,
    identity: ActivityAdmin,
    session: DbSession,
) -> ControlCentreResponse:
    """One read-only operational snapshot for the Owner/Trading Admin home."""

    del identity, session
    view = Day35ControlCentreService(get_session_factory()).read()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return ControlCentreResponse(
        generated_at=view.generated_at,
        overall_status=view.overall_status,
        open_signals=view.open_signals,
        pending_signals=view.pending_signals,
        trading_active_users=view.trading_active_users,
        trading_stopped_users=view.trading_stopped_users,
        source_live=view.source_live,
        source_testing=view.source_testing,
        source_paused=view.source_paused,
        source_revoked=view.source_revoked,
        telegram_connected=view.telegram_connected,
        telegram_attention=view.telegram_attention,
        mt5_connected=view.mt5_connected,
        mt5_attention=view.mt5_attention,
        active_users=view.active_users,
        invited_users=view.invited_users,
        suspended_users=view.suspended_users,
        revoked_users=view.revoked_users,
        review_open=view.review_open,
        review_open_24h=view.review_open_24h,
        message_errors_24h=view.message_errors_24h,
        publication_failed=view.publication_failed,
        publication_pending=view.publication_pending,
        push_failed_24h=view.push_failed_24h,
        telegram_notification_failed_24h=view.telegram_notification_failed_24h,
        live_board_ready=view.live_board_ready,
        live_board_pinned=view.live_board_pinned,
        review_stages=tuple(ReviewStageResponse(**asdict(item)) for item in view.review_stages),
        attention=tuple(AttentionResponse(**asdict(item)) for item in view.attention),
        recent_events=tuple(RecentEventResponse(**asdict(item)) for item in view.recent_events),
        broker_trade_action_created=view.broker_trade_action_created,
    )
