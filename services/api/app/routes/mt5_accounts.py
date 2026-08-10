"""Owner-only Day 22 Vantage MT5 demo connection routes."""

from __future__ import annotations

import os
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


@router.get("/demo/status", response_model=Mt5ConnectionResponse)
async def owner_demo_status(request: Request, response: Response, identity: OwnerIdentity) -> Mt5ConnectionResponse:
    service = require_mt5_service(request)
    _no_store(response)
    return _response(service.get_status(identity["id"]))


@router.post("/demo/connect", response_model=Mt5ConnectionResponse)
async def connect_owner_demo(payload: ConnectOwnerDemoRequest, request: Request, response: Response, identity: OwnerIdentity) -> Mt5ConnectionResponse:
    service = require_mt5_service(request)
    metaapi_token = os.getenv("SUPER_SIGNALS_METAAPI_TOKEN", "").strip()
    if len(metaapi_token) < 20:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "metaapi_platform_token_not_configured", "message": "The broker connection service is not configured yet."},
        )
    try:
        view = await service.connect_owner_demo(
            owner_user_id=identity["id"],
            metaapi_token=metaapi_token,
            login=payload.login,
            password=payload.password,
            server=payload.server,
        )
    except Mt5ConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": exc.code, "message": "The MT5 demo account could not be connected."}) from exc
    _no_store(response)
    return _response(view)


@router.post("/demo/refresh", response_model=Mt5ConnectionResponse)
async def refresh_owner_demo(request: Request, response: Response, identity: OwnerIdentity) -> Mt5ConnectionResponse:
    service = require_mt5_service(request)
    view = await service.refresh_owner_demo(identity["id"])
    _no_store(response)
    return _response(view)