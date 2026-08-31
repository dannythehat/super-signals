"""Member-facing saved Demo/Real MT5 account profiles and active-account switch."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.access_control import get_current_identity
from app.db import get_db_session
from app.member_routing_canonical import live_execution_enabled
from app.models import AuditEvent
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
        "mt5_profile_not_connected": "MetaAPI accepted the account but the broker session is still offline. Smart Signals will not mark it connected until the broker session is genuinely online.",
        "mt5_switch_open_exposure": "Close all Smart Signals open trades and pending orders before switching accounts.",
        "mt5_account_not_approved": "This Real MT5 account is not approved for this Smart Signals login.",
        "mt5_real_execution_disabled": "Real trading is temporarily unavailable. Keep Paper / Demo active for now.",
        "metaapi_e_auth": "MetaAPI reached Vantage, but the broker terminal rejected authentication for this account.",
        "metaapi_e_password_change_required": "Vantage requires the MT5 password to be changed before cloud connection can be enabled. Change it in MT5/Vantage, then reconnect.",
        "metaapi_err_otp_required": "This MT5 account requires a one-time password. MetaAPI cannot connect while MT5 OTP is required.",
        "metaapi_e_trading_account_disabled": "Vantage reports this MT5 account as disabled. Check the account status in Vantage.",
        "metaapi_e_srv_not_found": "MetaAPI could not find the exact Vantage server definition. The server name was accepted by Smart Signals but MetaAPI needs broker-server configuration.",
        "metaapi_e_server_timezone": "MetaAPI could not finish automatic Vantage server-settings detection. Smart Signals can retry this without changing your MT5 credentials.",
        "metaapi_e_resource_slots": "MetaAPI says this MT5 account needs additional connection resources before it can run in the cloud.",
        "metaapi_e_no_symbols": "Vantage returned no configured trading symbols for this MT5 account.",
        "metaapi_validationerror": "MetaAPI rejected the broker setup request. Smart Signals recorded the safe failure code for diagnosis.",
        "metaapi_account_rejected": "MetaAPI rejected the broker setup request before a live MT5 session was created. Smart Signals recorded this attempt for diagnosis.",
        "metaapi_timeout": "MT5 connectivity is temporarily unavailable. Try again in a moment.",
        "metaapi_unreachable": "MT5 connectivity is temporarily unavailable. Try again in a moment.",
        "metaapi_temporarily_unavailable": "MT5 connectivity is temporarily unavailable. Try again in a moment.",
        "metaapi_provisioning_timeout": "MT5 setup is still processing. Smart Signals will reuse the same broker account rather than create a duplicate.",
    }
    transient = {
        "metaapi_timeout",
        "metaapi_unreachable",
        "metaapi_temporarily_unavailable",
        "metaapi_provisioning_timeout",
        "metaapi_e_server_timezone",
    }
    raise HTTPException(
        status_code=(status.HTTP_503_SERVICE_UNAVAILABLE if exc.code in transient else status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code, "message": messages.get(exc.code, f"MT5 connection failed ({exc.code}).")},
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
        session.add(
            AuditEvent(
                actor_user_id=identity["id"],
                event_type="mt5.account_profile_connection_failed",
                entity_type="mt5_account_profile",
                payload={
                    "error_code": exc.code,
                    "account_environment": "demo" if "demo" in payload.server.casefold() else "live",
                    "login_last4": payload.login.strip()[-4:],
                    "server": payload.server.strip(),
                    "password_stored": False,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()
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