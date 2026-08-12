"""Normal-user Vantage MT5 live account linking for Day 30."""

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

router = APIRouter(prefix="/account/mt5", tags=["mt5-user"])
router.include_router(dashboard_day32_router)
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
                "message": "This MT5 setup route is for invited users only.",
            },
        )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _safe_message(code: str) -> str:
    messages = {
        "mt5_account_not_approved": "This MT5 login and server have not been approved by the owner.",
        "mt5_demo_not_available": "Demo MT5 accounts are not available for normal users.",
        "mt5_vantage_server_required": "Use the exact live Vantage MT5 server approved for your account.",
        "mt5_login_invalid": "Enter your Vantage MT5 account number.",
        "mt5_server_invalid": "Enter the exact Vantage MT5 server name.",
        "mt5_password_invalid": "Enter your MT5 trading password.",
        "metaapi_e_auth": "Vantage rejected the MT5 login, trading password or server.",
        "metaapi_platform_token_not_configured": "Super Signals MT5 connectivity needs administrator recovery.",
        "broker_credential_decryption_failed": "Super Signals MT5 connectivity needs administrator recovery.",
        "metaapi_timeout": "MT5 connectivity is temporarily unavailable. Use Refresh if the account was already linked, otherwise retry Connect.",
        "metaapi_unreachable": "MT5 connectivity is temporarily unavailable. Use Refresh if the account was already linked, otherwise retry Connect.",
        "metaapi_temporarily_unavailable": "MT5 connectivity is temporarily unavailable. Use Refresh if the account was already linked, otherwise retry Connect.",
        "metaapi_provisioning_timeout": "MT5 setup is still processing. Retry Connect; Super Signals will reuse the same broker account rather than create a duplicate.",
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
    approval = service.get_user_approval(user_id)
    data = asdict(view)
    return UserMt5StatusResponse(
        approved=approval.approved,
        configured=bool(data["configured"]),
        broker=str(data["broker"]),
        platform=str(data["platform"]),
        account_environment=str(data["account_environment"]),
        login_masked=data["login_masked"],
        server=data["server"] or approval.server,
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
    view = service.get_user_status(identity["id"])
    _no_store(response)
    return _response(service, identity["id"], view)


@router.post("/connect", response_model=UserMt5StatusResponse)
async def connect_user_mt5(
    payload: UserMt5ConnectRequest,
    request: Request,
    response: Response,
    identity: UserIdentity,
) -> UserMt5StatusResponse:
    _ordinary_user(identity)
    service = _service(request)
    try:
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
    view = await service.refresh_user_live(identity["id"])
    _no_store(response)
    return _response(service, identity["id"], view)
