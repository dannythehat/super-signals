"""Authenticated read-only mobile dashboard endpoint for Day 32."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.access_control import get_current_identity
from app.dashboard_day32 import Day32DashboardService, QuietDay23Mt5ReadService
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service

# Included by the existing /account/mt5 router, so the final endpoint is
# /account/mt5/dashboard without adding another application-level router.
router = APIRouter(prefix="/dashboard", tags=["dashboard-day32"])
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]


class ConnectionResponse(BaseModel):
    configured: bool
    status: str
    account_environment: str | None
    login_masked: str | None
    server: str | None
    error_code: str | None
    read_at: datetime | None


class AccountResponse(BaseModel):
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    trade_allowed: bool


class TradingResponse(BaseModel):
    available: bool
    status: str | None
    risk_percent: float | None
    allow_double_lot: bool | None
    effective_double_lot_risk_percent: float | None


class PerformanceResponse(BaseModel):
    key: str
    label: str
    amount: float | None
    known_position_count: int
    provisional_until_day33: bool


class OpenPositionResponse(BaseModel):
    position_id: UUID
    broker_position_id: str
    signal_id: UUID
    tp_index: int
    symbol: str
    side: str
    volume: float
    planned_risk_percent: float
    entry_price: float
    current_price: float | None
    stop_loss: float | None
    take_profit: float | None
    profit: float | None
    opened_at: datetime | None


class LatestSignalResponse(BaseModel):
    signal_id: UUID
    symbol: str
    side: str
    created_at: datetime
    position_count: int
    open_positions: int
    closed_positions: int
    status: str


class CompletedPositionResponse(BaseModel):
    position_id: UUID
    signal_id: UUID
    tp_index: int
    symbol: str
    side: str
    closed_at: datetime | None
    pnl_amount: float | None
    close_reason: str | None


class WinLossResponse(BaseModel):
    wins: int
    losses: int
    breakeven: int
    known_results: int
    win_rate_percent: float | None


class ActivityResponse(BaseModel):
    event_type: str
    label: str
    tone: str
    created_at: datetime


class DashboardResponse(BaseModel):
    connection: ConnectionResponse
    account: AccountResponse | None
    trading: TradingResponse
    open_profit: float | None
    open_positions: tuple[OpenPositionResponse, ...]
    latest_signal: LatestSignalResponse | None
    recent_completed: tuple[CompletedPositionResponse, ...]
    performance: tuple[PerformanceResponse, ...]
    win_loss: WinLossResponse
    activity: tuple[ActivityResponse, ...]
    reconciled_external_positions: int
    canonical_performance_ready: bool = False
    performance_basis: str = "provisional_position_records"
    broker_trade_action_created: bool = False


def _service(request: Request) -> Day32DashboardService:
    existing = getattr(request.app.state, "day32_dashboard_service", None)
    if isinstance(existing, Day32DashboardService):
        return existing

    base = require_mt5_service(request)
    if not isinstance(base, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "dashboard_mt5_runtime_unavailable",
                "message": "Account data is temporarily unavailable.",
            },
        )
    read_service = QuietDay23Mt5ReadService(
        session_factory=base._session_factory,
        cipher=base._cipher,
        gateway=MetaApiReadGateway(),
    )
    service = Day32DashboardService(
        session_factory=base._session_factory,
        read_service=read_service,
    )
    request.app.state.day32_dashboard_service = service
    return service


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _safe_open_profit(view: Any) -> float | None:
    if view.open_positions and all(item.profit is None for item in view.open_positions):
        return None
    return view.open_profit


@router.get("", response_model=DashboardResponse)
async def account_dashboard(
    request: Request,
    response: Response,
    identity: Identity,
) -> DashboardResponse:
    view = await _service(request).read(identity["id"])
    _no_store(response)
    return DashboardResponse(
        connection=ConnectionResponse(**asdict(view.connection)),
        account=(AccountResponse(**asdict(view.account)) if view.account is not None else None),
        trading=TradingResponse(**asdict(view.trading)),
        open_profit=_safe_open_profit(view),
        open_positions=tuple(OpenPositionResponse(**asdict(item)) for item in view.open_positions),
        latest_signal=(LatestSignalResponse(**asdict(view.latest_signal)) if view.latest_signal is not None else None),
        recent_completed=tuple(CompletedPositionResponse(**asdict(item)) for item in view.recent_completed),
        performance=tuple(PerformanceResponse(**asdict(item)) for item in view.performance),
        win_loss=WinLossResponse(**asdict(view.win_loss)),
        activity=tuple(ActivityResponse(**asdict(item)) for item in view.activity),
        reconciled_external_positions=view.reconciled_external_positions,
    )
