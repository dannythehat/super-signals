"""Owner-only Day 22 Vantage MT5 demo connection routes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.access_control import require_permission
from app.mt5_connection_service import Mt5ConnectionError, Mt5ConnectionView
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
