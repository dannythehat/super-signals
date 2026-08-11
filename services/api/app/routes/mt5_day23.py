"""Owner-only Day 23 live MT5 account-state endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.access_control import require_permission
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError

router = APIRouter(prefix="/owner/mt5", tags=["mt5"])
OwnerIdentity = Annotated[dict[str, Any], Depends(require_permission("mt5_accounts.approve"))]


class Day23AccountResponse(BaseModel):
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float | None
    leverage: float | None
    trade_allowed: bool


class Day23PriceResponse(BaseModel):
    symbol: str
    bid: float | None
    ask: float | None
    buy_price: float | None
    sell_price: float | None
    quote_time: datetime | None
    quote_age_seconds: float | None
    available: bool
    stale: bool
    execution_ready: bool
    block_reason: str | None


class Day23PositionResponse(BaseModel):
    position_id: str
    symbol: str
    side: str
    volume: float
    open_price: float
    current_price: float | None
    stop_loss: float | None
    take_profit: float | None
    profit: float | None
    swap: float | None
    commission: float | None
    opened_at: datetime | None
    updated_at: datetime | None


class Day23LiveStateResponse(BaseModel):
    login_masked: str
    server: str
    region: str
    read_at: datetime
    account: Day23AccountResponse
    price: Day23PriceResponse
    positions: list[Day23PositionResponse]
    execution_ready: bool
    execution_block_reason: str | None


def _service(request: Request) -> Day23Mt5ReadService:
    service = getattr(request.app.state, "day23_mt5_read_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "mt5_read_service_not_configured",
                "message": "The live MT5 read service is not configured.",
            },
        )
    return service


def _safe_message(code: str) -> str:
    messages = {
        "mt5_account_not_configured": "Connect the Vantage MT5 demo account first.",
        "mt5_account_not_connected": "The Vantage MT5 demo is not connected yet. Refresh the connection before reading live state.",
        "broker_credential_decryption_failed": "The permanent broker encryption configuration needs administrator recovery.",
        "metaapi_permission_denied": "MetaAPI refused the read. Check the MetaAPI balance/subscription before retrying.",
        "metaapi_timeout": "MetaAPI did not answer in time. Do not retry repeatedly.",
        "metaapi_unreachable": "MetaAPI is temporarily unreachable.",
        "metaapi_temporarily_unavailable": "MetaAPI is temporarily unavailable.",
        "metaapi_terminal_data_unavailable": "The MT5 terminal data or XAUUSD quote is not available from this connected account.",
        "metaapi_region_unavailable": "MetaAPI did not report a valid deployment region for this account.",
    }
    return messages.get(code, "The live MT5 account state could not be read.")


@router.get("/demo/live-state", response_model=Day23LiveStateResponse)
async def owner_demo_live_state(
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Day23LiveStateResponse:
    try:
        state = await _service(request).read_owner_live_state(identity["id"])
    except Day23ReadError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.retryable
                or exc.code
                in {
                    "broker_credential_decryption_failed",
                    "metaapi_permission_denied",
                    "metaapi_region_unavailable",
                    "metaapi_terminal_data_unavailable",
                }
                else status.HTTP_400_BAD_REQUEST
            ),
            detail={"code": exc.code, "message": _safe_message(exc.code)},
        ) from exc

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return Day23LiveStateResponse(
        login_masked=state.login_masked,
        server=state.server,
        region=state.region,
        read_at=state.read_at,
        account=Day23AccountResponse(**state.account.__dict__),
        price=Day23PriceResponse(**state.price.__dict__),
        positions=[Day23PositionResponse(**item.__dict__) for item in state.positions],
        execution_ready=state.execution_ready,
        execution_block_reason=state.execution_block_reason,
    )
