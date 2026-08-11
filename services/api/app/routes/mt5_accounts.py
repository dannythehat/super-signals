"""Owner-only Vantage MT5 demo connection and read-state routes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.access_control import require_permission
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_connection_service import Mt5ConnectionError, Mt5ConnectionView
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError
from app.mt5_runtime import require_mt5_service

router = APIRouter(prefix="/owner/mt5", tags=["mt5"])
OwnerIdentity = Annotated[dict[str, Any], Depends(require_permission("mt5_accounts.approve"))]


class ConnectOwnerDemoRequest(BaseModel):
    login: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    server: str = Field(min_length=2, max_length=160)


class Mt5ConnectionResponse(BaseModel):
    configured: bool
    account_id: str | None
    broker: str
    platform: str
    account_environment: str
    login_masked: str | None
    server: str | None
    status: str
    remote_state: str | None
    remote_connection_status: str | None
    last_error_code: str | None
    last_checked_at: datetime | None
    last_connected_at: datetime | None


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


def _response(view: Mt5ConnectionView) -> Mt5ConnectionResponse:
    data = asdict(view)
    data["account_id"] = str(view.account_id) if view.account_id is not None else None
    return Mt5ConnectionResponse(**data)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _safe_error_message(code: str) -> str:
    messages = {
        "metaapi_platform_token_not_configured": (
            "The broker platform credential is not available. An administrator must restore the permanent MT5 configuration."
        ),
        "broker_credential_decryption_failed": (
            "The stored broker credential could not be opened. Do not create a new account; restore the permanent encryption-key configuration."
        ),
        "metaapi_permission_denied": (
            "MetaAPI refused the request. Check the MetaAPI account balance/subscription before retrying."
        ),
        "metaapi_e_auth": (
            "Vantage rejected the MT5 credentials. Re-check the MT5 login, trading password and exact server name."
        ),
        "mt5_account_already_bound": (
            "This Super Signals user is already bound to a different MT5 login or server."
        ),
    }
    return messages.get(code, "The MT5 demo account could not be connected.")


def _day23_message(code: str) -> str:
    messages = {
        "mt5_account_not_configured": "Connect the Vantage MT5 demo account first.",
        "mt5_account_not_connected": "The Vantage MT5 demo is not connected yet. Refresh the connection before reading live state.",
        "broker_credential_decryption_failed": "The permanent broker encryption configuration needs administrator recovery.",
        "metaapi_terminal_scope_missing": (
            "The saved MetaAPI credential can manage the MT5 account but cannot read terminal data. Replace it once with the main MetaAPI API/admin token; Super Signals will store it encrypted and users will not manage MetaAPI credentials."
        ),
        "metaapi_permission_denied": "MetaAPI refused the read. Check the MetaAPI balance/subscription before retrying.",
        "metaapi_timeout": "MetaAPI did not answer in time. Do not retry repeatedly.",
        "metaapi_unreachable": "MetaAPI is temporarily unreachable.",
        "metaapi_temporarily_unavailable": "MetaAPI is temporarily unavailable.",
        "metaapi_terminal_data_unavailable": "The MT5 terminal data or XAUUSD quote is not available from this connected account.",
        "metaapi_region_unavailable": "MetaAPI did not report a valid deployment region for this account.",
    }
    return messages.get(code, "The live MT5 account state could not be read.")


def _day23_service(request: Request) -> Day23Mt5ReadService:
    cached = getattr(request.app.state, "day23_mt5_read_service", None)
    if cached is not None:
        return cached
    mt5_service = require_mt5_service(request)
    service = Day23Mt5ReadService(
        session_factory=mt5_service._session_factory,
        cipher=mt5_service._cipher,
        gateway=MetaApiReadGateway(),
    )
    request.app.state.day23_mt5_read_service = service
    return service


@router.get("/demo/status", response_model=Mt5ConnectionResponse)
async def owner_demo_status(
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Mt5ConnectionResponse:
    service = require_mt5_service(request)
    _no_store(response)
    return _response(service.get_status(identity["id"]))


@router.post("/demo/connect", response_model=Mt5ConnectionResponse)
async def connect_owner_demo(
    payload: ConnectOwnerDemoRequest,
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Mt5ConnectionResponse:
    service = require_mt5_service(request)
    try:
        metaapi_token = service.resolve_platform_token()
        view = await service.connect_owner_demo(
            owner_user_id=identity["id"],
            metaapi_token=metaapi_token,
            login=payload.login,
            password=payload.password,
            server=payload.server,
        )
    except Mt5ConnectionError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.code
                in {
                    "metaapi_platform_token_not_configured",
                    "broker_credential_decryption_failed",
                }
                else status.HTTP_400_BAD_REQUEST
            ),
            detail={
                "code": exc.code,
                "message": _safe_error_message(exc.code),
            },
        ) from exc
    _no_store(response)
    return _response(view)


@router.post("/demo/refresh", response_model=Mt5ConnectionResponse)
async def refresh_owner_demo(
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Mt5ConnectionResponse:
    service = require_mt5_service(request)
    view = await service.refresh_owner_demo(identity["id"])
    _no_store(response)
    return _response(view)


@router.get("/demo/live-state", response_model=Day23LiveStateResponse)
async def owner_demo_live_state(
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Day23LiveStateResponse:
    try:
        state = await _day23_service(request).read_owner_live_state(identity["id"])
    except Day23ReadError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.retryable
                or exc.code
                in {
                    "broker_credential_decryption_failed",
                    "metaapi_terminal_scope_missing",
                    "metaapi_permission_denied",
                    "metaapi_region_unavailable",
                    "metaapi_terminal_data_unavailable",
                }
                else status.HTTP_400_BAD_REQUEST
            ),
            detail={"code": exc.code, "message": _day23_message(exc.code)},
        ) from exc

    _no_store(response)
    return Day23LiveStateResponse(
        login_masked=state.login_masked,
        server=state.server,
        region=state.region,
        read_at=state.read_at,
        account=Day23AccountResponse(**asdict(state.account)),
        price=Day23PriceResponse(**asdict(state.price)),
        positions=[Day23PositionResponse(**asdict(item)) for item in state.positions],
        execution_ready=state.execution_ready,
        execution_block_reason=state.execution_block_reason,
    )
