"""Owner-only UI controls for manually closing mapped MT5 positions at market."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.access_control import get_current_identity
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.owner_manual_close import OwnerManualCloseError, OwnerManualCloseService

router = APIRouter(tags=["mt5-owner-manual-close"])
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]


class OwnerManualCloseResponse(BaseModel):
    requested_count: int
    closed_count: int
    already_closed_count: int
    failed_count: int
    closed_position_ids: tuple[UUID, ...]
    failed_position_ids: tuple[UUID, ...]
    broker_trade_action_created: bool = True


def _owner(identity: dict[str, Any]) -> None:
    if identity.get("role") != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "owner_manual_close_owner_only",
                "message": "Only the Owner Admin can manually close Smart Signals positions.",
            },
        )


def _service(request: Request) -> OwnerManualCloseService:
    existing = getattr(request.app.state, "owner_manual_close_service", None)
    if isinstance(existing, OwnerManualCloseService):
        return existing
    base = require_mt5_service(request)
    if not isinstance(base, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "owner_manual_close_unavailable",
                "message": "Manual close is temporarily unavailable.",
            },
        )
    service = OwnerManualCloseService(
        session_factory=base._session_factory,
        cipher=base._cipher,
        read_gateway=MetaApiReadGateway(),
        trade_gateway=MetaApiTradeGateway(),
    )
    request.app.state.owner_manual_close_service = service
    return service


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _http_error(exc: OwnerManualCloseError) -> HTTPException:
    if exc.code in {
        "owner_manual_position_not_open",
        "owner_manual_trade_not_open",
        "owner_manual_all_not_open",
    }:
        code = status.HTTP_409_CONFLICT
        message = "Those Smart Signals positions are no longer open. Refresh the account view."
    elif exc.code == "owner_manual_mt5_not_connected":
        code = status.HTTP_409_CONFLICT
        message = "The MT5 account is not connected."
    elif exc.code == "owner_manual_account_environment_invalid":
        code = status.HTTP_409_CONFLICT
        message = "This MT5 account has an unsupported account environment."
    elif exc.retryable:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
        message = "The broker did not confirm the close. No unconfirmed close is shown as completed."
    else:
        code = status.HTTP_502_BAD_GATEWAY
        message = "The broker could not confirm the manual close."
    return HTTPException(
        status_code=code,
        detail={"code": exc.code, "message": message},
    )


def _response(result: Any) -> OwnerManualCloseResponse:
    return OwnerManualCloseResponse(
        requested_count=result.requested_count,
        closed_count=result.closed_count,
        already_closed_count=result.already_closed_count,
        failed_count=result.failed_count,
        closed_position_ids=result.closed_position_ids,
        failed_position_ids=result.failed_position_ids,
        broker_trade_action_created=True,
    )


@router.post("/owner-close-position/{position_id}", response_model=OwnerManualCloseResponse)
async def owner_close_position(
    position_id: UUID,
    request: Request,
    response: Response,
    identity: Identity,
) -> OwnerManualCloseResponse:
    _owner(identity)
    try:
        result = await _service(request).close_position(identity["id"], position_id)
    except OwnerManualCloseError as exc:
        raise _http_error(exc) from exc
    _no_store(response)
    return _response(result)


@router.post("/owner-close-trade/{signal_id}", response_model=OwnerManualCloseResponse)
async def owner_close_trade(
    signal_id: UUID,
    request: Request,
    response: Response,
    identity: Identity,
) -> OwnerManualCloseResponse:
    _owner(identity)
    try:
        result = await _service(request).close_trade(identity["id"], signal_id)
    except OwnerManualCloseError as exc:
        raise _http_error(exc) from exc
    _no_store(response)
    return _response(result)


@router.post("/owner-close-all", response_model=OwnerManualCloseResponse)
async def owner_close_all(
    request: Request,
    response: Response,
    identity: Identity,
) -> OwnerManualCloseResponse:
    _owner(identity)
    try:
        result = await _service(request).close_all(identity["id"])
    except OwnerManualCloseError as exc:
        raise _http_error(exc) from exc
    _no_store(response)
    return _response(result)
