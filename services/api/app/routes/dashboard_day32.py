"""Authenticated read-only mobile dashboard endpoint for Day 32/33."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.acceptance_self_test import acceptance_mirror_owner_user_id
from app.access_control import get_current_identity
from app.dashboard_day32 import Day32DashboardService, QuietDay23Mt5ReadService
from app.dashboard_today_summary import TodayTradingSummaryService
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.routes.performance_day33 import (
    _service as _performance_service,
    router as performance_day33_router,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard-day32"])
router.include_router(performance_day33_router)
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


class TodayTradingSummaryResponse(BaseModel):
    timezone: str
    trades: int
    wins: int
    losses: int
    breakeven: int
    open: int
    pending: int
    settling: int
    realised_pnl: float
    winning_pips: float
    net_pips: float
    session_started_at: datetime | None = None
    broker_trade_action_created: bool = False


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
    canonical_performance_ready: bool
    performance_basis: str = "broker_deal_ledger"
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


@router.get("/today", response_model=TodayTradingSummaryResponse)
def account_dashboard_today(
    request: Request,
    response: Response,
    identity: Identity,
    timezone_name: str = "UTC",
) -> TodayTradingSummaryResponse:
    service = _service(request)
    mirror_user_id = acceptance_mirror_owner_user_id(identity, service._session_factory)
    data_user_id = mirror_user_id or identity["id"]
    summary = TodayTradingSummaryService(session_factory=service._session_factory).read(
        data_user_id,
        timezone_name=timezone_name,
    )
    _no_store(response)
    return TodayTradingSummaryResponse(
        timezone=summary.timezone,
        session_started_at=summary.session_started_at,
        trades=summary.trades,
        wins=summary.wins,
        losses=summary.losses,
        breakeven=summary.breakeven,
        open=summary.open,
        pending=summary.pending,
        settling=summary.settling,
        realised_pnl=float(summary.realised_pnl),
        winning_pips=float(summary.winning_pips),
        net_pips=float(summary.net_pips),
    )


@router.get("", response_model=DashboardResponse)
async def account_dashboard(
    request: Request,
    response: Response,
    identity: Identity,
) -> DashboardResponse:
    service = _service(request)
    mirror_user_id = acceptance_mirror_owner_user_id(identity, service._session_factory)
    data_user_id = mirror_user_id or identity["id"]
    view = await service.read(data_user_id)
    if mirror_user_id is not None:
        view = replace(
            view,
            connection=replace(view.connection, account_environment="live"),
            trading=service._trading(identity["id"]),
            reconciled_external_positions=0,
        )

    ledger = _performance_service(request)
    ready = ledger.ledger_ready(data_user_id)
    windows = {item.key: item for item in ledger.read_windows(data_user_id)} if ready else {}

    period_labels = (("today", "Today"), ("7d", "7 days"), ("30d", "30 days"))
    canonical_periods = tuple(
        PerformanceResponse(
            key=key,
            label=(windows[key].label if key in windows else label),
            amount=(float(windows[key].cash_pnl) if key in windows else None),
            known_position_count=(windows[key].closed_trades if key in windows else 0),
            provisional_until_day33=False,
        )
        for key, label in period_labels
    )

    all_time = windows.get("all")
    win_loss = (
        WinLossResponse(
            wins=all_time.wins,
            losses=all_time.losses,
            breakeven=all_time.breakeven,
            known_results=all_time.wins + all_time.losses + all_time.breakeven,
            win_rate_percent=(
                float(all_time.win_rate_percent)
                if all_time.win_rate_percent is not None
                else None
            ),
        )
        if all_time is not None
        else WinLossResponse(
            wins=0,
            losses=0,
            breakeven=0,
            known_results=0,
            win_rate_percent=None,
        )
    )
    _no_store(response)
    return DashboardResponse(
        connection=ConnectionResponse(**asdict(view.connection)),
        account=(AccountResponse(**asdict(view.account)) if view.account is not None else None),
        trading=TradingResponse(**asdict(view.trading)),
        open_profit=_safe_open_profit(view),
        open_positions=tuple(OpenPositionResponse(**asdict(item)) for item in view.open_positions),
        latest_signal=(LatestSignalResponse(**asdict(view.latest_signal)) if view.latest_signal is not None else None),
        recent_completed=tuple(CompletedPositionResponse(**asdict(item)) for item in view.recent_completed),
        performance=canonical_periods,
        win_loss=win_loss,
        activity=tuple(ActivityResponse(**asdict(item)) for item in view.activity),
        reconciled_external_positions=view.reconciled_external_positions,
        canonical_performance_ready=ready,
    )
