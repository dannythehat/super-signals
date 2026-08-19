"""Authenticated canonical broker performance and timeline endpoints."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from app.acceptance_self_test import acceptance_mirror_owner_user_id
from app.access_control import get_current_identity
from app.admin_portfolio_day35 import Day35AdminPortfolioService, PeriodKey, SortKey
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.performance_ledger_day33 import Day33LedgerError
from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2
from app.performance_runtime import CanonicalPerformanceRuntimeService

router = APIRouter(prefix="/performance", tags=["performance-day33"])
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]


class SyncResponse(BaseModel):
    user_id: UUID
    broker_deals_added: int
    mapped_positions_checked: int
    outcomes_rebuilt: int
    summaries_rebuilt: int
    broker_trade_action_created: bool


class ReadinessResponse(BaseModel):
    canonical_performance_ready: bool
    performance_basis: str = "broker_deal_ledger"
    broker_trade_action_created: bool = False


class PerformanceWindowResponse(BaseModel):
    key: str
    label: str
    cash_pnl: float
    return_percent: float | None
    model_500_pnl: float
    model_500_return_percent: float
    closed_trades: int
    wins: int
    losses: int
    breakeven: int
    open_trades: int
    win_rate_percent: float | None
    net_pips: float | None
    mixed_instrument_pips: bool


class TimelineTradeResponse(BaseModel):
    signal_id: UUID
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
    cash_pnl: float | None
    net_pips: float | None
    model_500_pnl: float | None
    close_reason: str | None


class TimelineResponse(BaseModel):
    trades: tuple[TimelineTradeResponse, ...]
    open_count: int
    pending_count: int
    provider_identity_visible: bool
    broker_trade_action_created: bool


class LiveBoardTradeResponse(BaseModel):
    signal_id: UUID
    symbol: str
    side: str
    status: str
    status_label: str
    open_positions: int
    pending_positions: int
    position_count: int


class LiveBoardResponse(BaseModel):
    open_count: int
    pending_count: int
    trades: tuple[LiveBoardTradeResponse, ...]
    provider_identity_visible: bool = False
    broker_trade_action_created: bool = False


class AdminPortfolioRowResponse(BaseModel):
    dimension_type: str
    source_id: UUID
    source_label: str
    trader_stream: str | None
    source_color_index: int
    realized_cash_pnl: float
    open_cash_pnl: float | None
    open_cash_pnl_known: bool
    return_percent: float | None
    trades_closed: int
    trades_open: int
    wins: int
    losses: int
    breakeven: int
    win_rate_percent: float | None
    net_pips: float | None
    rank: int


class AdminPortfolioResponse(BaseModel):
    period_key: str
    period_label: str
    period_start: datetime
    period_end: datetime
    rows: tuple[AdminPortfolioRowResponse, ...]
    performance_basis: str
    provider_identity_visible: bool
    broker_trade_action_created: bool
    open_cash_pnl_live: bool
    open_cash_pnl_as_of: datetime | None
    open_cash_pnl_error_code: str | None


def _service(request: Request) -> Day33PerformanceLedgerServiceV2:
    existing = getattr(request.app.state, "day33_performance_service", None)
    if isinstance(existing, CanonicalPerformanceRuntimeService):
        return existing
    base = require_mt5_service(request)
    if not isinstance(base, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "performance_mt5_runtime_unavailable",
                "message": "Performance data is temporarily unavailable.",
            },
        )
    service = CanonicalPerformanceRuntimeService(
        session_factory=base._session_factory,
        cipher=base._cipher,
        gateway=MetaApiReadGateway(),
    )
    request.app.state.day33_performance_service = service
    return service


def _read_user_id(request: Request, identity: dict[str, Any]) -> UUID:
    service = _service(request)
    return acceptance_mirror_owner_user_id(identity, service._session_factory) or identity["id"]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _raise_ledger(exc: Day33LedgerError) -> None:
    raise HTTPException(
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE
            if exc.retryable
            or exc.code
            in {
                "mt5_account_not_connected",
                "broker_credential_decryption_failed",
                "metaapi_permission_denied",
                "metaapi_token_invalid",
            }
            else status.HTTP_400_BAD_REQUEST
        ),
        detail={
            "code": exc.code,
            "message": "Broker-backed performance is temporarily unavailable.",
        },
    ) from exc


def _admin_portfolio_role(identity: dict[str, Any]) -> None:
    if str(identity.get("role") or "") not in {"owner", "trading_admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "day35_admin_portfolio_forbidden",
                "message": "The Signal Portfolio is available to Super Signals administrators only.",
            },
        )


@router.post("/sync", response_model=SyncResponse)
async def sync_performance(
    request: Request,
    response: Response,
    identity: Identity,
) -> SyncResponse:
    try:
        result = await _service(request).sync_user(identity["id"])
    except Day33LedgerError as exc:
        _raise_ledger(exc)
    _no_store(response)
    return SyncResponse(**asdict(result))


@router.get("/readiness", response_model=ReadinessResponse)
async def performance_readiness(
    request: Request,
    response: Response,
    identity: Identity,
) -> ReadinessResponse:
    _no_store(response)
    return ReadinessResponse(
        canonical_performance_ready=_service(request).ledger_ready(_read_user_id(request, identity))
    )


@router.get("/windows", response_model=tuple[PerformanceWindowResponse, ...])
async def performance_windows(
    request: Request,
    response: Response,
    identity: Identity,
) -> tuple[PerformanceWindowResponse, ...]:
    windows = _service(request).read_windows(_read_user_id(request, identity))
    _no_store(response)
    return tuple(PerformanceWindowResponse(**asdict(item)) for item in windows)


@router.get("/timeline", response_model=TimelineResponse)
async def performance_timeline(
    request: Request,
    response: Response,
    identity: Identity,
    status_value: str | None = Query(default=None, alias="status"),
    source: UUID | None = Query(default=None),
    trader: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=100, ge=1, le=250),
) -> TimelineResponse:
    view = _service(request).read_timeline(
        _read_user_id(request, identity),
        viewer_role=str(identity.get("role") or "user"),
        status_filter=status_value,
        source_filter=source,
        trader_filter=trader,
        limit=limit,
    )
    _no_store(response)
    return TimelineResponse(
        trades=tuple(TimelineTradeResponse(**asdict(item)) for item in view.trades),
        open_count=view.open_count,
        pending_count=view.pending_count,
        provider_identity_visible=view.provider_identity_visible,
        broker_trade_action_created=view.broker_trade_action_created,
    )


@router.get("/live-board", response_model=LiveBoardResponse)
async def live_board_contract(
    request: Request,
    response: Response,
    identity: Identity,
) -> LiveBoardResponse:
    _ = identity
    rows = _service(request).read_shared_live_board()
    trades: list[LiveBoardTradeResponse] = []
    for row in rows:
        open_positions = int(row["open_positions"])
        pending_positions = int(row["pending_positions"])
        is_open = open_positions > 0
        trades.append(
            LiveBoardTradeResponse(
                signal_id=row["signal_id"],
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                status="open" if is_open else "pending",
                status_label="Open" if is_open else "Pending",
                open_positions=open_positions,
                pending_positions=pending_positions,
                position_count=int(row["position_count"]),
            )
        )
    _no_store(response)
    return LiveBoardResponse(
        open_count=sum(1 for item in trades if item.status == "open"),
        pending_count=sum(1 for item in trades if item.status == "pending"),
        trades=tuple(trades),
    )


@router.get("/admin-portfolio", response_model=AdminPortfolioResponse)
async def admin_signal_portfolio(
    request: Request,
    response: Response,
    identity: Identity,
    period: str = Query(default="today"),
    sort_by: str = Query(default="realized_pnl"),
) -> AdminPortfolioResponse:
    _admin_portfolio_role(identity)
    allowed_periods = {"today", "7d", "month", "year", "all"}
    allowed_sorts = {"realized_pnl", "return_percent", "win_rate", "trade_count"}
    if period not in allowed_periods or sort_by not in allowed_sorts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "day35_admin_portfolio_filter_invalid",
                "message": "Choose a supported Signal Portfolio period and sort order.",
            },
        )

    view = await Day35AdminPortfolioService(_service(request)).read_with_live_open_pnl(
        cast(PeriodKey, period),
        sort_by=cast(SortKey, sort_by),
    )
    _no_store(response)
    return AdminPortfolioResponse(
        period_key=view.period_key,
        period_label=view.period_label,
        period_start=view.period_start,
        period_end=view.period_end,
        rows=tuple(AdminPortfolioRowResponse(**asdict(item)) for item in view.rows),
        performance_basis=view.performance_basis,
        provider_identity_visible=view.provider_identity_visible,
        broker_trade_action_created=view.broker_trade_action_created,
        open_cash_pnl_live=view.open_cash_pnl_live,
        open_cash_pnl_as_of=view.open_cash_pnl_as_of,
        open_cash_pnl_error_code=view.open_cash_pnl_error_code,
    )
