"""Authenticated read-only mobile dashboard endpoint for Day 32/33."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.acceptance_self_test import acceptance_mirror_owner_user_id
from app.access_control import get_current_identity
from app.dashboard_day32 import QuietDay23Mt5ReadService
from app.dashboard_runtime import (
    CanonicalDashboardRuntimeService,
    CanonicalTodayTradingSummaryService,
)
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_token_scope import inspect_metaapi_token_scope
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.mt5_runtime import require_mt5_service
from app.paper_resilient_read_gateway import ResilientMetaApiReadGateway
from app.routes.performance_day33 import (
    _service as _performance_service,
    router as performance_day33_router,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard-day32"])
router.include_router(performance_day33_router)
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]

_TERMINAL_BROKER_ORDER_STATES = {
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_REJECTED",
    "ORDER_STATE_EXPIRED",
    "ORDER_STATE_FILLED",
}


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
    win_loss: WinLossResponse
    activity: tuple[ActivityResponse, ...]
    reconciled_external_positions: int
    canonical_performance_ready: bool
    performance_basis: str = "selected_provider_broker_ledger"
    broker_trade_action_created: bool = False


def _service(request: Request) -> CanonicalDashboardRuntimeService:
    existing = getattr(request.app.state, "day32_dashboard_service", None)
    if isinstance(existing, CanonicalDashboardRuntimeService):
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
        gateway=ResilientMetaApiReadGateway(),
    )
    service = CanonicalDashboardRuntimeService(
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


def _terminal_broker_order_ids(history: list[dict[str, object]]) -> set[str]:
    """Return tickets that immutable broker history proves are no longer pending."""
    terminal: set[str] = set()
    for item in history:
        order_id = str(item.get("id") or "").strip()
        if not order_id:
            continue
        state = str(item.get("state") or "").strip().upper()
        done_time = str(item.get("doneTime") or "").strip()
        if state in _TERMINAL_BROKER_ORDER_STATES or done_time:
            terminal.add(order_id)
    return terminal


def _broker_history_proves_terminal(
    order_id: str,
    history: list[dict[str, object]],
) -> bool:
    """True only when immutable broker history proves this ticket is no longer pending."""
    return order_id in _terminal_broker_order_ids(history)


async def _active_broker_order_ids(
    service: CanonicalDashboardRuntimeService,
    user_id: UUID,
    *,
    history_start: datetime,
) -> set[str] | None:
    """Return broker-verified active pending-entry tickets, or None when unavailable.

    MetaAPI's current ``/orders`` replica can lag terminal MT5 state, so current pending
    entries are cross-checked against one bulk immutable broker-history read. Broker
    history wins when a ticket has already filled/cancelled/rejected/expired.

    This deliberately performs at most two broker reads for the normal dashboard path:
    one current-order read and one bulk history read. It must never make one network
    request per order ticket.
    """
    read_service = service._read_service
    row = read_service._load_row(user_id)
    if row is None:
        return None
    try:
        token = read_service._cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))
    except BrokerCredentialDecryptionError:
        return None

    scope = inspect_metaapi_token_scope(token)
    if (
        scope.jwt_payload_decoded
        and scope.is_explicitly_narrowed
        and not scope.has_terminal_access
    ):
        return None

    account_id = str(row["metaapi_account_id"])
    try:
        region = await read_service._gateway.resolve_account_region(
            token=token,
            account_id=account_id,
        )
        orders = await read_service._gateway.read_orders(
            token=token,
            account_id=account_id,
            region=region,
        )
        current_order_ids = {
            str(item.get("id") or "").strip()
            for item in orders
            if str(item.get("id") or "").strip()
        }
        if not current_order_ids:
            return set()

        history = await read_service._gateway.read_history_orders_by_time_range(
            token=token,
            account_id=account_id,
            region=region,
            start_time=history_start,
            end_time=datetime.now(UTC),
            offset=0,
            limit=1000,
        )
        if len(history) >= 1000:
            return None
        return current_order_ids - _terminal_broker_order_ids(history)
    except MetaApiGatewayError:
        return None


def _broker_pending_trade_count(
    service: CanonicalDashboardRuntimeService,
    user_id: UUID,
    *,
    session_started_at: datetime,
    active_order_ids: set[str] | None,
) -> int | None:
    if active_order_ids is None:
        return None
    if not active_order_ids:
        return 0
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
                  AND p.broker_order_id = ANY(:active_order_ids)
                  AND p.created_at>=:session_started_at
                  AND src.status<>'revoked'
                """
            ),
            {
                "user_id": user_id,
                "active_order_ids": list(active_order_ids),
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
    mirror_user_id = acceptance_mirror_owner_user_id(identity, service._session_factory)
    data_user_id = mirror_user_id or identity["id"]
    summary = CanonicalTodayTradingSummaryService(
        session_factory=service._session_factory
    ).read(
        data_user_id,
        timezone_name=timezone_name,
    )
    active_order_ids = await _active_broker_order_ids(
        service,
        data_user_id,
        history_start=summary.session_started_at,
    )
    pending = _broker_pending_trade_count(
        service,
        data_user_id,
        session_started_at=summary.session_started_at,
        active_order_ids=active_order_ids,
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
        pending=pending,
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
