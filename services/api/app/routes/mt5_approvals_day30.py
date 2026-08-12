"""Owner approval for one live Vantage MT5 login/server per invited user."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.access_control import require_permission
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service

router = APIRouter(prefix="/owner/mt5/approvals", tags=["mt5-approvals"])
OwnerIdentity = Annotated[
    dict[str, Any],
    Depends(require_permission("mt5_accounts.approve")),
]


class ApproveUserMt5Request(BaseModel):
    login: str = Field(min_length=1, max_length=32)
    server: str = Field(min_length=2, max_length=160)


class UserMt5ApprovalResponse(BaseModel):
    approved: bool
    approval_id: UUID | None
    user_id: UUID
    broker: str = "vantage"
    platform: str = "mt5"
    account_environment: str = "live"
    login_masked: str | None
    server: str | None
    status: str
    approved_at: datetime | None
    updated_at: datetime | None


def _service(request: Request) -> Day30Mt5ConnectionService:
    service = require_mt5_service(request)
    if not isinstance(service, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "mt5_user_linking_not_configured", "message": "MT5 user approvals are not available yet."},
        )
    return service


def _owner(identity: dict[str, Any]) -> None:
    if identity.get("role") != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "owner_required", "message": "Only the Owner can approve a user's MT5 account."},
        )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _response(view) -> UserMt5ApprovalResponse:
    return UserMt5ApprovalResponse(
        approved=view.approved,
        approval_id=view.approval_id,
        user_id=view.user_id,
        login_masked=view.login_masked,
        server=view.server,
        status=view.status,
        approved_at=view.approved_at,
        updated_at=view.updated_at,
    )


def _raise_approval(exc: Mt5ConnectionError) -> None:
    messages = {
        "mt5_login_invalid": "Enter the user's Vantage MT5 account number.",
        "mt5_server_invalid": "Enter the exact live Vantage MT5 server.",
        "mt5_vantage_server_required": "Only Vantage MT5 accounts can be approved.",
        "mt5_demo_not_available": "Normal users can only be approved for live Vantage accounts.",
        "mt5_user_not_eligible": "Only an active invited User account can receive an MT5 approval.",
    }
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": exc.code, "message": messages.get(exc.code, "The MT5 account could not be approved.")},
    ) from exc


@router.get("/{user_id}", response_model=UserMt5ApprovalResponse)
async def get_user_mt5_approval(
    user_id: UUID,
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> UserMt5ApprovalResponse:
    _owner(identity)
    view = _service(request).get_user_approval(user_id)
    _no_store(response)
    return _response(view)


@router.post("/{user_id}", response_model=UserMt5ApprovalResponse)
async def approve_user_mt5(
    user_id: UUID,
    payload: ApproveUserMt5Request,
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> UserMt5ApprovalResponse:
    _owner(identity)
    service = _service(request)
    try:
        view = service.approve_user_account(
            approver_user_id=identity["id"],
            user_id=user_id,
            login=payload.login,
            server=payload.server,
        )
    except Mt5ConnectionError as exc:
        _raise_approval(exc)
    _no_store(response)
    return _response(view)
