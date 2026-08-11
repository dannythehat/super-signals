"""Owner-only Day 26 V1 exact/zone multi-position demo execution route."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.access_control import require_permission
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.mt5_runtime import require_mt5_service

router = APIRouter(prefix="/owner/mt5/day26", tags=["mt5", "day26"])
OwnerIdentity = Annotated[dict[str, Any], Depends(require_permission("mt5_accounts.approve"))]


class Day26ExecuteRequest(BaseModel):
    signal_id: UUID
    risk_percent: str = Field(pattern=r"^(0\.5|1|1\.5|2)$")
    double_lot_approved: bool = False


class Day26PositionResponse(BaseModel):
    local_position_id: UUID
    tp_index: int
    take_profit: Decimal | None
    volume: Decimal
    client_id: str
    broker_order_id: str
    broker_position_id: str
    broker_open_price: Decimal


class Day26ExecutionResponse(BaseModel):
    signal_id: UUID
    user_id: UUID
    symbol: str
    side: str
    signal_entry_price: Decimal
    stop_loss: Decimal
    base_risk_percent: Decimal
    effective_risk_percent: Decimal
    double_lot_applied: bool
    positions: list[Day26PositionResponse]


def _service(request: Request) -> AtomicDay26Mt5ExecutionService:
    cached = getattr(request.app.state, "day26_mt5_execution_service", None)
    if cached is not None:
        return cached
    mt5_service = require_mt5_service(request)
    service = AtomicDay26Mt5ExecutionService(
        session_factory=mt5_service._session_factory,
        cipher=mt5_service._cipher,
        read_gateway=MetaApiReadGateway(),
        margin_gateway=MetaApiMarginGateway(),
        trade_gateway=MetaApiTradeGateway(),
    )
    request.app.state.day26_mt5_execution_service = service
    return service


def _safe_message(code: str) -> str:
    messages = {
        "day26_market_signal_required": "Day 26 V1 supports market signals only; pending orders are skipped.",
        "day26_demo_account_required": "Day 26 can execute only on the connected Vantage demo account.",
        "entry_price_unavailable": "The live XAUUSD price does not equal the provider's exact entry, so no order was sent.",
        "zone_not_reached": "The live XAUUSD price did not enter the provider's zone within the five-minute V1 window.",
        "signal_changed_before_execution": "The provider edited the signal before execution; this attempt was stopped so the latest version can be used.",
        "signal_no_longer_accepted": "The provider's latest signal version is no longer execution-eligible.",
        "signal_cancelled": "The provider cancelled the setup before execution.",
        "insufficient_funds": "The broker-reported free margin is insufficient for the complete position set.",
        "day26_partial_execution_rollback_failed": "A partial submission could not be fully compensated. Trading is blocked until the broker state is reconciled.",
        "mt5_account_not_configured": "The Vantage demo account is not configured.",
        "mt5_account_not_connected": "The Vantage demo account is not connected.",
    }
    return messages.get(code, "Day 26 demo execution was stopped safely.")


def _status_for(code: str) -> int:
    if code == "day26_partial_execution_rollback_failed":
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if code.startswith("metaapi_") or code.startswith("broker_"):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_409_CONFLICT


@router.post("/execute", response_model=Day26ExecutionResponse)
async def execute_day26_demo_signal(
    payload: Day26ExecuteRequest,
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Day26ExecutionResponse:
    """Execute one existing canonical V1 signal through Days 23-26 on demo only."""
    try:
        result = await _service(request).execute_owner_demo_signal(
            owner_user_id=identity["id"],
            signal_id=payload.signal_id,
            risk_percent=payload.risk_percent,
            double_lot_approved=payload.double_lot_approved,
        )
    except Day26ExecutionError as exc:
        raise HTTPException(
            status_code=_status_for(exc.code),
            detail={"code": exc.code, "message": _safe_message(exc.code)},
        ) from exc

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return Day26ExecutionResponse(
        signal_id=result.signal_id,
        user_id=result.user_id,
        symbol=result.symbol,
        side=result.side,
        signal_entry_price=result.signal_entry_price,
        stop_loss=result.stop_loss,
        base_risk_percent=result.base_risk_percent,
        effective_risk_percent=result.effective_risk_percent,
        double_lot_applied=result.double_lot_applied,
        positions=[
            Day26PositionResponse(
                local_position_id=item.local_position_id,
                tp_index=item.tp_index,
                take_profit=item.take_profit,
                volume=item.volume,
                client_id=item.client_id,
                broker_order_id=item.broker_order_id,
                broker_position_id=item.broker_position_id,
                broker_open_price=item.broker_open_price,
            )
            for item in result.positions
        ],
    )
