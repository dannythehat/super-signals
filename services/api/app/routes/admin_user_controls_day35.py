"""Day 35 Owner member management and confirmed emergency controls."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.access_control import require_permission
from app.admin_user_controls_day35 import (
    Day35AdminControlError,
    Day35AdminUserControlService,
)
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.trading_controls_day31 import Day31TradingControlService

router = APIRouter(prefix="/user-controls", tags=["day35-admin-controls"])
OwnerUsers = Annotated[dict[str, Any], Depends(require_permission("users.manage"))]
EmergencyAdmin = Annotated[
    dict[str, Any], Depends(require_permission("emergency_stop.use"))
]


class ManagedUserResponse(BaseModel):
    user_id: UUID
    email: str
    display_name: str | None
    status: str
    trading_status: str | None
    risk_percent: Decimal | None
    allow_double_lot: bool | None
    mt5_status: str | None
    mt5_login_masked: str | None
    mt5_server: str | None
    mapped_open_positions: int
    mapped_pending_positions: int
    active_sessions: int
    push_devices_enabled: int


class RevokePreviewResponse(BaseModel):
    user: ManagedUserResponse
    confirmation_text: str
    confirmation_title: str
    confirmation_message: str
    mapped_positions_to_close: int
    manual_or_unmapped_positions_touched: bool
    broker_trade_action_created: bool


class ConfirmedActionRequest(BaseModel):
    confirmed: bool = False
    confirmation_text: str = Field(default="", max_length=400)


class RevokeResultResponse(BaseModel):
    user_id: UUID
    status: str
    automation_status: str
    broker_actions_sent: int
    mapped_positions_closed: int
    external_positions_reconciled: int
    sessions_revoked: int
    approvals_revoked: int
    push_devices_disabled: int
    manual_or_unmapped_positions_touched: bool


class EmergencyPreviewResponse(BaseModel):
    active_users: int
    automation_active_users: int
    mapped_open_positions: int
    confirmation_text: str
    confirmation_title: str
    confirmation_message: str
    manual_or_unmapped_positions_touched: bool
    broker_trade_action_created: bool


class EmergencyResultResponse(BaseModel):
    users_targeted: int
    users_completed: int
    users_failed: int
    automation_users_stopped_first: int
    broker_actions_sent: int
    mapped_positions_closed: int
    external_positions_reconciled: int
    failures: tuple[dict[str, str], ...]
    manual_or_unmapped_positions_touched: bool


def _service(request: Request) -> Day35AdminUserControlService:
    existing = getattr(request.app.state, "day35_admin_user_control_service", None)
    if isinstance(existing, Day35AdminUserControlService):
        return existing

    base = require_mt5_service(request)
    if not isinstance(base, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "day35_admin_controls_unavailable",
                "message": "Administrative trading controls are temporarily unavailable.",
            },
        )
    trading = Day31TradingControlService(
        session_factory=base._session_factory,
        cipher=base._cipher,
        read_gateway=MetaApiReadGateway(),
        trade_gateway=MetaApiTradeGateway(),
    )
    service = Day35AdminUserControlService(
        session_factory=base._session_factory,
        trading_service=trading,
    )
    request.app.state.day35_admin_user_control_service = service
    return service


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _raise_admin_error(exc: Day35AdminControlError) -> None:
    messages = {
        "day35_managed_user_not_found": "That invited user was not found.",
        "day35_user_not_active": "Only an active invited user can be revoked from this control.",
        "day35_revoke_confirmation_required": "Type the exact revoke confirmation phrase before continuing.",
        "day35_owner_self_revoke_blocked": "The Owner account cannot revoke itself here.",
        "day35_user_state_changed": "The user's state changed while the revoke was running. Refresh before retrying.",
        "day35_emergency_confirmation_required": "Type the exact emergency-stop confirmation phrase before continuing.",
        "mt5_account_not_connected": "The user's approved Vantage MT5 account is not connected. Automation remains stopped; retry the account action after broker connectivity is restored.",
        "broker_credential_decryption_failed": "MT5 connectivity needs administrator recovery. Automation remains stopped.",
    }
    transient = {
        "mt5_account_not_connected",
        "metaapi_timeout",
        "metaapi_unreachable",
        "metaapi_temporarily_unavailable",
    }
    raise HTTPException(
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE
            if exc.retryable or exc.code in transient
            else status.HTTP_409_CONFLICT
        ),
        detail={
            "code": exc.code,
            "message": messages.get(exc.code, "The confirmed administrative action could not be completed safely."),
        },
    ) from exc


@router.get("/users", response_model=tuple[ManagedUserResponse, ...])
def managed_users(
    request: Request,
    response: Response,
    actor: OwnerUsers,
) -> tuple[ManagedUserResponse, ...]:
    del actor
    rows = _service(request).list_users()
    _no_store(response)
    return tuple(ManagedUserResponse(**asdict(item)) for item in rows)


@router.get("/users/{user_id}/revoke-preview", response_model=RevokePreviewResponse)
def revoke_preview(
    user_id: UUID,
    request: Request,
    response: Response,
    actor: OwnerUsers,
) -> RevokePreviewResponse:
    del actor
    try:
        view = _service(request).revoke_preview(user_id)
    except Day35AdminControlError as exc:
        _raise_admin_error(exc)
    _no_store(response)
    return RevokePreviewResponse(
        user=ManagedUserResponse(**asdict(view.user)),
        confirmation_text=view.confirmation_text,
        confirmation_title=view.confirmation_title,
        confirmation_message=view.confirmation_message,
        mapped_positions_to_close=view.mapped_positions_to_close,
        manual_or_unmapped_positions_touched=view.manual_or_unmapped_positions_touched,
        broker_trade_action_created=view.broker_trade_action_created,
    )


@router.post("/users/{user_id}/revoke", response_model=RevokeResultResponse)
async def revoke_user(
    user_id: UUID,
    payload: ConfirmedActionRequest,
    request: Request,
    response: Response,
    actor: OwnerUsers,
) -> RevokeResultResponse:
    try:
        result = await _service(request).revoke_user(
            user_id=user_id,
            actor_user_id=actor["id"],
            confirmed=payload.confirmed,
            confirmation_text=payload.confirmation_text,
        )
    except Day35AdminControlError as exc:
        _raise_admin_error(exc)
    _no_store(response)
    return RevokeResultResponse(**asdict(result))


@router.get("/emergency-preview", response_model=EmergencyPreviewResponse)
def emergency_preview(
    request: Request,
    response: Response,
    actor: EmergencyAdmin,
) -> EmergencyPreviewResponse:
    del actor
    view = _service(request).emergency_preview()
    _no_store(response)
    return EmergencyPreviewResponse(**asdict(view))


@router.post("/emergency-stop", response_model=EmergencyResultResponse)
async def emergency_stop(
    payload: ConfirmedActionRequest,
    request: Request,
    response: Response,
    actor: EmergencyAdmin,
) -> EmergencyResultResponse:
    try:
        result = await _service(request).emergency_stop(
            actor_user_id=actor["id"],
            confirmed=payload.confirmed,
            confirmation_text=payload.confirmation_text,
        )
    except Day35AdminControlError as exc:
        _raise_admin_error(exc)
    _no_store(response)
    return EmergencyResultResponse(**asdict(result))
