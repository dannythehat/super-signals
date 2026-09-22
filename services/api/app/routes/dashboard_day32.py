"""Authenticated read-only mobile dashboard endpoint for Day 32/33."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.access_control import get_current_identity
from app.dashboard_day32 import QuietDay23Mt5ReadService
from app.dashboard_resilient_runtime import ResilientDashboardRuntimeService
from app.dashboard_runtime import CanonicalTodayTradingSummaryService
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.paper_resilient_read_gateway import ResilientMetaApiReadGateway
from app.routes.performance_day33 import (
    _service as _performance_service,
    router as performance_day33_router,
)
from app.trading_accounting import CanonicalTradingAccountingService

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


class DailyProfitResponse(BaseModel):
    day: date
    pnl: float
    opening_balance: float
    return_percent: float


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
    pending: int | None
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
    performance_timezone: str = "UTC"
    daily_profit: tuple[DailyProfitResponse, ...] = ()
    win_loss: WinLossResponse
    activity: tuple[ActivityResponse, ...]
    reconciled_external_positions: int
    canonical_performance_ready: bool
    performance_basis: str = "canonical_user_trading_ledger"
    broker_trade_action_created: bool = False


def _service(request: Request) -> ResilientDashboardRuntimeService:
    existing = getattr(request.app.state, "day32_dashboard_service", None)
    if isinstance(existing, ResilientDashboardRuntimeService):
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
        gateway=ResilientMetaApiReadGateway(timeout_seconds=2.5, attempts=1),
    )
    service = ResilientDashboardRuntimeService(
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


def _local_pending_trade_count(
    service: ResilientDashboardRuntimeService,
    user_id: UUID,
    *,
    session_started_at: datetime,
) -> int:
    """Count the reconciled local pending ledger without another MetaAPI round trip."""
    with service._session_factory() as session:
        value = session.execute(
            text(
                """
                SELECT COUNT(DISTINCT p.signal_id)::int
                FROM positions AS p
                JOIN signals AS s ON s.id=p.signal_id
                JOIN sources AS src ON src.id=s.source_id
                WHERE p.user_id=:user_id
                  AND p.status='pending'
                  AND p.broker_order_id IS NOT NULL
                  AND p.created_at>=:session_started_at
                  AND src.status<>'revoked'
                """
            ),
            {
                "user_id": user_id,
                "session_started_at": session_started_at,
            },
        ).scalar_one()
    return int(value or 0)


@router.get("/today", response_model=TodayTradingSummaryResponse)
async def account_dashboard_today(
    request: Request,
    response: Response,
    identity: Identity,
    timezone_name: str = "UTC",
) -> TodayTradingSummaryResponse:
    service = _service(request)
    summary = CanonicalTodayTradingSummaryService(
        session_factory=service._session_factory
    ).read(
        identity["id"],
        timezone_name=timezone_name,
    )
    accounting_windows = CanonicalTradingAccountingService(
        service._session_factory
    ).windows(
        identity["id"],
        timezone_name=timezone_name,
    )
    pending = _local_pending_trade_count(
        service,
        identity["id"],
        session_started_at=summary.session_started_at,
    )
    _no_store(response)
    return TodayTradingSummaryResponse(
        timezone=accounting_windows.timezone,
        session_started_at=summary.session_started_at,
        trades=summary.trades,
        wins=summary.wins,
        losses=summary.losses,
        breakeven=summary.breakeven,
        open=summary.open,
        pending=pending,
        settling=summary.settling,
        realised_pnl=float(accounting_windows.today),
        winning_pips=float(summary.winning_pips),
        net_pips=float(summary.net_pips),
    )


@router.get("", response_model=DashboardResponse)
async def account_dashboard(
    request: Request,
    response: Response,
    identity: Identity,
    timezone_name: str = "UTC",
) -> DashboardResponse:
    service = _service(request)
    view = await service.read(identity["id"])

    accounting = CanonicalTradingAccountingService(service._session_factory)
    money_windows = accounting.windows(
        identity["id"],
        timezone_name=timezone_name,
    )
    canonical_periods = (
        PerformanceResponse(
            key="today",
            label="Today",
            amount=float(money_windows.today),
            known_position_count=0,
            provisional_until_day33=False,
        ),
        PerformanceResponse(
            key="week",
            label="This week",
            amount=float(money_windows.week),
            known_position_count=0,
            provisional_until_day33=False,
        ),
        PerformanceResponse(
            key="month",
            label="This month",
            amount=float(money_windows.month),
            known_position_count=0,
            provisional_until_day33=False,
        ),
        PerformanceResponse(
            key="all",
            label="All time",
            amount=float(money_windows.all_time),
            known_position_count=0,
            provisional_until_day33=False,
        ),
    )
    daily_profit = (
        tuple(
            DailyProfitResponse(
                day=item.day,
                pnl=float(item.pnl),
                opening_balance=float(item.opening_balance),
                return_percent=float(item.return_percent),
            )
            for item in accounting.daily(
                identity["id"],
                broker_account_value=view.account.equity,
                timezone_name=timezone_name,
            )
        )
        if view.account is not None
        else ()
    )

    ledger = _performance_service(request)
    ready = ledger.ledger_ready(identity["id"])
    old_windows = {
        item.key: item for item in ledger.read_windows(identity["id"])
    } if ready else {}
    all_time = old_windows.get("all")
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
        account=(
            AccountResponse(**asdict(view.account)) if view.account is not None else None
        ),
        trading=TradingResponse(**asdict(view.trading)),
        open_profit=_safe_open_profit(view),
        open_positions=tuple(
            OpenPositionResponse(**asdict(item)) for item in view.open_positions
        ),
        latest_signal=(
            LatestSignalResponse(**asdict(view.latest_signal))
            if view.latest_signal is not None
            else None
        ),
        recent_completed=tuple(
            CompletedPositionResponse(**asdict(item)) for item in view.recent_completed
        ),
        performance=canonical_periods,
        performance_timezone=money_windows.timezone,
        daily_profit=daily_profit,
        win_loss=win_loss,
        activity=tuple(ActivityResponse(**asdict(item)) for item in view.activity),
        reconciled_external_positions=view.reconciled_external_positions,
        canonical_performance_ready=True,
    )
