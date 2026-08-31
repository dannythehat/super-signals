"""Canonical member Vantage MT5 account routes for Smart Signals."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.access_control import get_current_identity
from app.mt5_connection_service import Mt5ConnectionView
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.routes.dashboard_day32 import router as dashboard_day32_router
from app.routes.gold_quote import router as gold_quote_router
from app.routes.mt5_account_profiles import router as mt5_account_profiles_router

router = APIRouter(prefix="/account/mt5", tags=["mt5-user"])
router.include_router(dashboard_day32_router)
router.include_router(gold_quote_router)
router.include_router(mt5_account_profiles_router)
UserIdentity = Annotated[dict[str, Any], Depends(get_current_identity)]


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
