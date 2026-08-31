"""Member-facing saved Demo/Real MT5 account profiles and active-account switch."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.access_control import get_current_identity
from app.db import get_db_session
from app.member_routing_canonical import live_execution_enabled
from app.mt5_account_profiles import Mt5AccountProfileService
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.subscription_access import require_active_subscription

router = APIRouter(prefix="/profiles", tags=["mt5-user-profiles"])
UserIdentity = Annotated[dict[str, Any], Depends(get_current_identity)]
DbSession = Annotated[Session, Depends(get_db_session)]


class ProfileConnectRequest(BaseModel):
    login: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    server: str = Field(min_length=2, max_length=160)
    make_active: bool = False


class ProfileActivateRequest(BaseModel):
    account_environment: str = Field(pattern="^(demo|live)$")


def _connection_service(request: Request) -> Day30Mt5ConnectionService:
    service = require_mt5_service(request)
    if not isinstance(service, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "mt5_user_linking_not_configured", "message": "MT5 account linking is temporarily unavailable."},
        )
    return service


def _profiles(request: Request) -> Mt5AccountProfileService:
    connection = _connection_service(request)
    return Mt5AccountProfileService(
        session_factory=connection._session_factory,  # noqa: SLF001 - same runtime service boundary
        connection_service=connection,
    )


def _eligible(identity: dict[str, Any]) -> str:
    role = str(identity.get("role") or "").lower()
    if role not in {"user", "owner", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "mt5_account_switch_not_allowed", "message": "This account cannot manage a trading account."},
        )
    return role


def _require_member_subscription(session: Session, identity: dict[str, Any], role: str) -> None:
    if role == "user":
        require_active_subscription(session, identity["id"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _raise_profile_error(exc: Mt5ConnectionError) -> None:
    messages = {
        "mt5_vantage_server_required": "Use the exact Vantage MT5 server shown by Vantage.",
        "mt5_login_invalid": "Enter your Vantage MT5 account number.",
        "mt5_server_invalid": "Enter the exact Vantage MT5 server name.",
        "mt5_password_invalid": "Enter your MT5 trading password.",
        "mt5_profile_not_configured": "Connect this account before switching to it.",
        "mt5_profile_not_connected": "This MT5 account is not connected yet. Reconnect it before switching.",
        "mt5_switch_open_exposure": "Close all Smart Signals open trades and pending orders before switching accounts.",
        "mt5_account_not_approved": "This Real MT5 account is not approved for this Smart Signals login.",
        "mt5_real_execution_disabled": "Real trading is temporarily unavailable. Keep Paper / Demo active for now.",
        "metaapi_e_auth": "Vantage rejected the MT5 account number, trading password or server.",
        "metaapi_timeout": "MT5 connectivity is temporarily unavailable. Try again in a moment.",
        "metaapi_unreachable": "MT5 connectivity is temporarily unavailable. Try again in a moment.",
        "metaapi_temporarily_unavailable": "MT5 connectivity is temporarily unavailable. Try again in a moment.",
        "metaapi_provisioning_timeout": "MT5 setup is still processing. Try again in a moment.",
    }
    transient = {
        "metaapi_timeout",
        "metaapi_unreachable",
        "metaapi_temporarily_unavailable",
        "metaapi_provisioning_timeout",
    }
    raise HTTPException(
        status_code=(status.HTTP_503_SERVICE_UNAVAILABLE if exc.code in transient else status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code, "message": messages.get(exc.code, "The MT5 account action could not be completed.")},
    ) from exc


def _decorate(result: dict[str, Any]) -> dict[str, Any]:
    return {**result, "real_execution_enabled": live_execution_enabled()}


@router.get("")
async def list_mt5_profiles(
    request: Request,
    response: Response,
    identity: UserIdentity,
) -> dict[str, Any]:
    _eligible(identity)
    result = _profiles(request).list_profiles(identity["id"])
    _no_store(response)
    return _decorate(result)


@router.post("/connect")
async def connect_mt5_profile(
    payload: ProfileConnectRequest,
    request: Request,
    response: Response,
    identity: UserIdentity,
    session: DbSession,
) -> dict[str, Any]:
    role = _eligible(identity)
    _require_member_subscription(session, identity, role)
    try:
        result = await _profiles(request).connect_profile(
            user_id=identity["id"],
            role=role,
            login=payload.login,
            password=payload.password,
            server=payload.server,
            make_active=payload.make_active,
        )
    except Mt5ConnectionError as exc:
        _raise_profile_error(exc)
    _no_store(response)
    return _decorate(result)


@router.post("/activate")
async def activate_mt5_profile(
    payload: ProfileActivateRequest,
    request: Request,
    response: Response,
    identity: UserIdentity,
    session: DbSession,
) -> dict[str, Any]:
    role = _eligible(identity)
    _require_member_subscription(session, identity, role)
    if payload.account_environment == "live" and not live_execution_enabled():
        _raise_profile_error(Mt5ConnectionError("mt5_real_execution_disabled"))
    try:
        result = _profiles(request).activate_profile(
            user_id=identity["id"],
            role=role,
            environment=payload.account_environment,
        )
    except Mt5ConnectionError as exc:
        _raise_profile_error(exc)
    _no_store(response)
    return _decorate(result)
