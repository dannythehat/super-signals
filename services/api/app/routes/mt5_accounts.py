"""Owner-only Day 22 Vantage MT5 demo connection routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.access_control import require_permission
from app.mt5_connection_service import Mt5ConnectionError, Mt5ConnectionView

router = APIRouter(prefix="/owner/mt5", tags=["mt5"])
OwnerIdentity = Annotated[dict[str, Any], Depends(require_permission("mt5_accounts.approve"))]


class ConnectOwnerDemoRequest(BaseModel):
    metaapi_token: str = Field(min_length=20, max_length=4096)
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
    last_checked_at: object | None
    last_connected_at: object | None


def _service(request: Request):
    return request.app.state.mt5_connection_service


def _response(view: Mt5ConnectionView) -> Mt5ConnectionResponse:
    data = asdict(view)
    data["account_id"] = str(view.account_id) if view.account_id is not None else None
    return Mt5ConnectionResponse(**data)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.get("/demo/status", response_model=Mt5ConnectionResponse)
async def owner_demo_status(
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Mt5ConnectionResponse:
    _no_store(response)
    return _response(_service(request).get_status(identity["id"]))


@router.post("/demo/connect", response_model=Mt5ConnectionResponse)
async def connect_owner_demo(
    payload: ConnectOwnerDemoRequest,
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Mt5ConnectionResponse:
    try:
        view = await _service(request).connect_owner_demo(
            owner_user_id=identity["id"],
            metaapi_token=payload.metaapi_token,
            login=payload.login,
            password=payload.password,
            server=payload.server,
        )
    except Mt5ConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": "The MT5 demo account could not be connected."},
        ) from exc
    _no_store(response)
    return _response(view)


@router.post("/demo/refresh", response_model=Mt5ConnectionResponse)
async def refresh_owner_demo(
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> Mt5ConnectionResponse:
    view = await _service(request).refresh_owner_demo(identity["id"])
    _no_store(response)
    return _response(view)
