"""Invited-user risk and automated trading controls for Day 31."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.access_control import get_current_identity, require_permission
from app.db import get_db_session
from app.dual_account_trading_controls import DualAccountTradingControlService
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.subscription_access import require_active_subscription
from app.trading_controls_day31 import (
    Day31ActivationPreview,
    Day31StopResult,
    Day31TradingControlError,
    Day31TradingControlService,
    Day31TradingControlView,
)

router = APIRouter(prefix="/trading", tags=["trading-controls"])
UserIdentity = Annotated[dict[str, Any], Depends(get_current_identity)]
RiskIdentity = Annotated[dict[str, Any], Depends(require_permission("risk.manage"))]
AutomationIdentity = Annotated[
    dict[str, Any], Depends(require_permission("automation.toggle"))
]
DbSession = Annotated[Session, Depends(get_db_session)]


class RiskSettingsRequest(BaseModel):
    risk_percent: Decimal = Field()
    allow_double_lot: bool


class ActivateRequest(BaseModel):
    confirmed: bool


class RequirementResponse(BaseModel):
    key: str
    label: str
    passed: bool


class TradingSettingsResponse(BaseModel):
    risk_percent: Decimal
    allow_double_lot: bool
    effective_normal_risk_percent: Decimal
    effective_double_lot_risk_percent: Decimal
    trading_status: str


class TradingControlResponse(TradingSettingsResponse):
    recommended_risk_percent: Decimal = Decimal("1")
    recommended_allow_double_lot: bool = True
    allowed_risk_percents: tuple[Decimal, ...] = (
        Decimal("0.5"), Decimal("1"), Decimal("1.5"), Decimal("2")
    )


class ActivationPreviewResponse(BaseModel):
    settings: TradingControlResponse
    ready: bool
    requirements: tuple[RequirementResponse, ...]
    confirmation_title: str
    confirmation_message: str


class StopCloseResponse(BaseModel):
    settings: TradingControlResponse
    broker_actions_sent: int
    positions_closed: int
    external_positions_reconciled: int
    already_stopped: bool


def _ordinary_user(identity: dict[str, Any]) -> None:
    if identity.get("role") != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "trading_user_route_only",
                "message": "These trading controls are for invited users only.",
            },
        )


def _service(request: Request) -> Day31TradingControlService:
    existing = getattr(request.app.state, "day31_trading_control_service", None)
    if isinstance(existing, DualAccountTradingControlService):
        return existing

    base = require_mt5_service(request)
    if not isinstance(base, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "trading_controls_not_configured",
                "message": "Trading controls are temporarily unavailable.",
            },
        )
    service = DualAccountTradingControlService(
        session_factory=base._session_factory,
        cipher=base._cipher,
        read_gateway=MetaApiReadGateway(),
        trade_gateway=MetaApiTradeGateway(),
    )
    request.app.state.day31_trading_control_service = service
    return service


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _settings(view: Day31TradingControlView) -> TradingControlResponse:
    return TradingControlResponse(
        risk_percent=view.risk_percent,
        allow_double_lot=view.allow_double_lot,
        effective_normal_risk_percent=view.effective_normal_risk_percent,
        effective_double_lot_risk_percent=view.effective_double_lot_risk_percent,
        trading_status=view.trading_status,
    )


def _preview(view: Day31ActivationPreview) -> ActivationPreviewResponse:
    return ActivationPreviewResponse(
        settings=_settings(view.settings),
        ready=view.ready,
        requirements=tuple(RequirementResponse(**item) for item in view.requirements),
        confirmation_title=view.confirmation_title,
        confirmation_message=view.confirmation_message,
    )


def _stop(view: Day31StopResult) -> StopCloseResponse:
    return StopCloseResponse(
        settings=_settings(view.settings),
        broker_actions_sent=view.broker_actions_sent,
        positions_closed=view.positions_closed,
        external_positions_reconciled=view.external_positions_reconciled,
        already_stopped=view.already_stopped,
    )


def _raise_control_error(exc: Day31TradingControlError) -> None:
    messages = {
        "risk_percent_invalid": "Choose 0.5%, 1%, 1.5% or 2% risk per position.",
        "activation_confirmation_required": "Confirm the effective risk before activating automated trading.",
        "activation_requirements_incomplete": "Complete the required account and MT5 setup before activating trades.",
        "trading_user_not_eligible": "This account is not eligible for automated trading.",
        "mt5_account_not_connected": "The selected Vantage MT5 account is not connected.",
        "broker_credential_decryption_failed": "MT5 connectivity needs administrator recovery.",
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
            if exc.code in transient
            else status.HTTP_400_BAD_REQUEST
        ),
        detail={
            "code": exc.code,
            "message": messages.get(exc.code, "The trading control action could not be completed."),
        },
    ) from exc


@router.get("", response_model=TradingControlResponse)
def get_trading_controls(
    request: Request,
    response: Response,
    identity: UserIdentity,
) -> TradingControlResponse:
    _ordinary_user(identity)
    try:
        view = _service(request).get_settings(identity["id"])
    except Day31TradingControlError as exc:
        _raise_control_error(exc)
    _no_store(response)
    return _settings(view)


@router.patch("/risk", response_model=TradingControlResponse)
def update_trading_risk(
    payload: RiskSettingsRequest,
    request: Request,
    response: Response,
    identity: RiskIdentity,
) -> TradingControlResponse:
    _ordinary_user(identity)
    try:
        view = _service(request).update_risk(
            user_id=identity["id"],
            risk_percent=payload.risk_percent,
            allow_double_lot=payload.allow_double_lot,
        )
    except Day31TradingControlError as exc:
        _raise_control_error(exc)
    _no_store(response)
    return _settings(view)


@router.get("/activation-preview", response_model=ActivationPreviewResponse)
def activation_preview(
    request: Request,
    response: Response,
    identity: AutomationIdentity,
    session: DbSession,
) -> ActivationPreviewResponse:
    _ordinary_user(identity)
    require_active_subscription(session, identity["id"])
    try:
        view = _service(request).activation_preview(identity["id"])
    except Day31TradingControlError as exc:
        _raise_control_error(exc)
    _no_store(response)
    return _preview(view)


@router.post("/activate", response_model=TradingControlResponse)
def activate_trading(
    payload: ActivateRequest,
    request: Request,
    response: Response,
    identity: AutomationIdentity,
    session: DbSession,
) -> TradingControlResponse:
    _ordinary_user(identity)
    require_active_subscription(session, identity["id"])
    try:
        view = _service(request).activate(
            user_id=identity["id"], confirmed=payload.confirmed
        )
    except Day31TradingControlError as exc:
        _raise_control_error(exc)
    _no_store(response)
    return _settings(view)


@router.post("/stop-and-close", response_model=StopCloseResponse)
async def stop_and_close_trading(
    request: Request,
    response: Response,
    identity: AutomationIdentity,
) -> StopCloseResponse:
    _ordinary_user(identity)
    try:
        view = await _service(request).stop_and_close(identity["id"])
    except Day31TradingControlError as exc:
        _raise_control_error(exc)
    _no_store(response)
    return _stop(view)
