"""Owner-only Day 27 provider follow-up execution route."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.access_control import require_permission
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_management_day27 import Day27ManagementError, Day27Mt5ManagementService
from app.mt5_runtime import require_mt5_service

router = APIRouter(prefix="/owner/mt5/day27", tags=["mt5", "day27"])
OwnerIdentity = Annotated[dict[str, Any], Depends(require_permission("mt5_accounts.approve"))]


class Day27ManageRequest(BaseModel):
    lifecycle_event_id: UUID


class Day27ManagementResponse(BaseModel):
    lifecycle_event_id: UUID
    signal_id: UUID
    user_id: UUID
    actions_requested: int
    broker_actions_sent: int
    positions_closed: int
    positions_modified: int
    orders_cancelled: int
    external_positions_reconciled: int
    already_applied: bool


def _service(request: Request) -> Day27Mt5ManagementService:
    cached = getattr(request.app.state, "day27_mt5_management_service", None)
    if cached is not None:
        return cached
    mt5_service = require_mt5_service(request)
    service = Day27Mt5ManagementService(
        session_factory=mt5_service._session_factory,
        cipher=mt5_service._cipher,
        read_gateway=MetaApiReadGateway(),
        trade_gateway=MetaApiTradeGateway(),
    )
    request.app.state.day27_mt5_management_service = service
    return service


def _safe_message(code: str) -> str:
    messages = {
        "day27_lifecycle_event_not_found": "The linked provider follow-up event was not found.",
        "day27_management_action_missing": "The provider update does not contain a supported explicit management instruction.",
        "day27_management_action_unsupported": "This provider management instruction is not supported in Day 27.",
        "day27_management_target_unsupported": "The provider management target could not be mapped safely.",
        "day27_stop_loss_value_invalid": "The provider stop-loss instruction does not contain a valid explicit price.",
        "day27_take_profit_value_invalid": "The provider take-profit instruction does not contain a valid explicit price.",
        "day27_demo_account_required": "Day 27 can execute only on the connected Vantage demo account.",
        "mt5_account_not_configured": "The Vantage demo account is not configured.",
        "mt5_account_not_connected": "The Vantage demo account is not connected.",
    }
    return messages.get(code, "Day 27 management was stopped safely.")


def _status_for(code: str) -> int:
    if code.startswith("metaapi_") or code.startswith("broker_"):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_409_CONFLICT


@router.post("/manage", response_model=Day27ManagementResponse)
async def execute_day27_management(
    payload: Day27ManageRequest,
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Day27ManagementResponse:
    """Apply one canonical provider follow-up event to mapped Vantage demo positions."""
    try:
        result = await _service(request).execute_owner_demo_event(
            owner_user_id=identity["id"],
            lifecycle_event_id=payload.lifecycle_event_id,
        )
    except Day27ManagementError as exc:
        raise HTTPException(
            status_code=_status_for(exc.code),
            detail={"code": exc.code, "message": _safe_message(exc.code)},
        ) from exc

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return Day27ManagementResponse(
        lifecycle_event_id=result.lifecycle_event_id,
        signal_id=result.signal_id,
        user_id=result.user_id,
        actions_requested=result.actions_requested,
        broker_actions_sent=result.broker_actions_sent,
        positions_closed=result.positions_closed,
        positions_modified=result.positions_modified,
        orders_cancelled=result.orders_cancelled,
        external_positions_reconciled=result.external_positions_reconciled,
        already_applied=result.already_applied,
    )
