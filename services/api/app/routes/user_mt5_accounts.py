"""Normal-user Vantage MT5 account linking for Smart Signals onboarding."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.access_control import get_current_identity
from app.mt5_connection_service import Mt5ConnectionError, Mt5ConnectionView
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.routes.dashboard_day32 import router as dashboard_day32_router
from app.routes.gold_quote import router as gold_quote_router

router = APIRouter(prefix="/account/mt5", tags=["mt5-user"])
router.include_router(dashboard_day32_router)
router.include_router(gold_quote_router)
UserIdentity = Annotated[dict[str, Any], Depends(get_current_identity)]


class UserMt5ConnectRequest(BaseModel):
    login: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    server: str = Field(min_length=2, max_length=160)


class UserMt5StatusResponse(BaseModel):
    approved: bool
    configured: bool
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


def _service(request: Request) -> Day30Mt5ConnectionService:
    service = require_mt5_service(request)
    if not isinstance(service, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "mt5_user_linking_not_configured",
                "message": "MT5 account linking is not available yet.",
            },
        )
    return service


def _ordinary_user(identity: dict[str, Any]) -> None:
    if identity.get("role") != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "mt5_user_route_only",
                "message": "This MT5 setup route is for Smart Signals member accounts.",
            },
        )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _safe_message(code: str) -> str:
    messages = {
        "mt5_account_not_approved": "This Vantage MT5 account could not be authorised for your Smart Signals login.",
        "mt5_vantage_server_required": "Use the exact Vantage MT5 server shown in your Vantage welcome email or Client Portal.",
        "mt5_login_invalid": "Enter your Vantage MT5 account number.",
        "mt5_server_invalid": "Enter the exact Vantage MT5 server name.",
        "mt5_password_invalid": "Enter your MT5 trading password.",
        "mt5_account_already_bound": "A different MT5 account is already connected. Disconnect or replace it before adding another.",
        "metaapi_e_auth": "Vantage rejected the MT5 account number, trading password or server.",
        "metaapi_platform_token_not_configured": "Smart Signals MT5 connectivity needs administrator recovery.",
        "broker_credential_decryption_failed": "Smart Signals MT5 connectivity needs administrator recovery.",
        "metaapi_timeout": "MT5 connectivity is temporarily unavailable. Retry Connect in a moment.",
        "metaapi_unreachable": "MT5 connectivity is temporarily unavailable. Retry Connect in a moment.",
        "metaapi_temporarily_unavailable": "MT5 connectivity is temporarily unavailable. Retry Connect in a moment.",
        "metaapi_provisioning_timeout": "MT5 setup is still processing. Retry Connect; Smart Signals will reuse the same broker account rather than create a duplicate.",
    }
    return messages.get(code, "The Vantage MT5 account could not be linked.")


def _raise_connection(exc: Mt5ConnectionError) -> None:
    transient = {
        "metaapi_timeout",
        "metaapi_unreachable",
        "metaapi_temporarily_unavailable",
        "metaapi_provisioning_timeout",
        "metaapi_platform_token_not_configured",
        "broker_credential_decryption_failed",
    }
    raise HTTPException(
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE
            if exc.code in transient
            else status.HTTP_400_BAD_REQUEST
        ),
        detail={"code": exc.code, "message": _safe_message(exc.code)},
    ) from exc


def _response(
    service: Day30Mt5ConnectionService,
    user_id,
    view: Mt5ConnectionView,
) -> UserMt5StatusResponse:
    data = asdict(view)
    environment = str(data["account_environment"])
    approval = service.get_user_approval(user_id) if environment == "live" else None
    return UserMt5StatusResponse(
        approved=(approval.approved if approval is not None else bool(data["configured"])),
        configured=bool(data["configured"]),
        broker=str(data["broker"]),
        platform=str(data["platform"]),
        account_environment=environment,
        login_masked=data["login_masked"],
        server=data["server"] or (approval.server if approval is not None else None),
        status=str(data["status"]),
        remote_state=data["remote_state"],
        remote_connection_status=data["remote_connection_status"],
        last_error_code=data["last_error_code"],
        last_checked_at=data["last_checked_at"],
        last_connected_at=data["last_connected_at"],
    )


@router.get("/status", response_model=UserMt5StatusResponse)
async def user_mt5_status(
    request: Request,
    response: Response,
    identity: UserIdentity,
) -> UserMt5StatusResponse:
    _ordinary_user(identity)
    service = _service(request)
    view = service.get_status(identity["id"])
    _no_store(response)
    return _response(service, identity["id"], view)


@router.post("/connect", response_model=UserMt5StatusResponse)
async def connect_user_mt5(
    payload: UserMt5ConnectRequest,
    request: Request,
    response: Response,
    identity: UserIdentity,
) -> UserMt5StatusResponse:
    """Connect the signed-in user's own Vantage MT5 account.

    Demo Vantage servers are treated as Paper accounts. Live Vantage servers are
    self-authorised for the same signed-in user before using the established live
    connection service. The user's MT5 password is never persisted by Smart Signals.
    """
    _ordinary_user(identity)
    service = _service(request)
    server_folded = payload.server.strip().casefold()

    try:
        if "demo" in server_folded:
            token = service.resolve_platform_token()
            view = await service.connect_owner_demo(
                owner_user_id=identity["id"],
                metaapi_token=token,
                login=payload.login,
                password=payload.password,
                server=payload.server,
            )
        else:
            # The customer is authorising their own broker account, so there is no
            # separate owner approval step in the public website onboarding flow.
            service.approve_user_account(
                approver_user_id=identity["id"],
                user_id=identity["id"],
                login=payload.login,
                server=payload.server,
            )
            view = await service.connect_user_live(
                user_id=identity["id"],
                login=payload.login,
                password=payload.password,
                server=payload.server,
            )
    except Mt5ConnectionError as exc:
        _raise_connection(exc)

    _no_store(response)
    return _response(service, identity["id"], view)


@router.post("/refresh", response_model=UserMt5StatusResponse)
async def refresh_user_mt5(
    request: Request,
    response: Response,
    identity: UserIdentity,
) -> UserMt5StatusResponse:
    _ordinary_user(identity)
    service = _service(request)
    current = service.get_status(identity["id"])
    if current.account_environment == "demo":
        view = await service.refresh_owner_demo(identity["id"])
    else:
        view = await service.refresh_user_live(identity["id"])
    _no_store(response)
    return _response(service, identity["id"], view)
