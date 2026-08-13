"""Day 35 administrator trades and failures drill-down."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from app.access_control import require_permission
from app.operations_day35 import Day35OperationsService
from app.routes.performance_day33 import _service as performance_service

router = APIRouter(prefix="/operations", tags=["day35-operations"])
ActivityAdmin = Annotated[dict[str, Any], Depends(require_permission("activity.view"))]


class AdminTradeResponse(BaseModel):
    signal_id: UUID
    public_reference: str
    public_marker: str
    symbol: str
    side: str
    status: str
    status_label: str
    status_color: str
    source_label: str | None
    trader_stream: str | None
    source_color_index: int | None
    opened_at: datetime | None
    closed_at: datetime | None
    position_count: int
    open_positions: int
    pending_positions: int
    closed_positions: int
    cash_pnl: Decimal | None
    net_pips: Decimal | None
    model_500_pnl: Decimal | None
    close_reason: str | None
    telegram_root_published: bool


class CurrentFailureResponse(BaseModel):
    failure_type: str
    severity: str
    title: str
    detail: str
    failure_code: str | None
    occurred_at: datetime
    signal_id: UUID | None
    public_reference: str | None
    source_label: str | None


class FailureHistoryResponse(BaseModel):
    event_type: str
    entity_type: str
    error_code: str | None
    created_at: datetime


class OperationsResponse(BaseModel):
    generated_at: datetime
    trades: tuple[AdminTradeResponse, ...]
    current_failures: tuple[CurrentFailureResponse, ...]
    recent_failure_history: tuple[FailureHistoryResponse, ...]
    current_failure_count: int
    provider_identity_visible: bool
    broker_trade_action_created: bool


@router.get("", response_model=OperationsResponse)
def day35_operations(
    request: Request,
    response: Response,
    actor: ActivityAdmin,
    trade_limit: int = Query(default=200, ge=1, le=250),
    failure_limit: int = Query(default=100, ge=1, le=200),
) -> OperationsResponse:
    del actor
    ledger = performance_service(request)
    view = Day35OperationsService(
        session_factory=ledger._session_factory,
        ledger=ledger,
    ).read(trade_limit=trade_limit, failure_limit=failure_limit)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return OperationsResponse(
        generated_at=view.generated_at,
        trades=tuple(AdminTradeResponse(**asdict(item)) for item in view.trades),
        current_failures=tuple(CurrentFailureResponse(**asdict(item)) for item in view.current_failures),
        recent_failure_history=tuple(FailureHistoryResponse(**asdict(item)) for item in view.recent_failure_history),
        current_failure_count=view.current_failure_count,
        provider_identity_visible=view.provider_identity_visible,
        broker_trade_action_created=view.broker_trade_action_created,
    )
