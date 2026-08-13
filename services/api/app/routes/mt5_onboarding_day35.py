"""Member MT5 approval-request and Owner review routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.access_control import get_current_identity, require_permission
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_onboarding_day35 import Day35Mt5OnboardingService
from app.mt5_runtime import require_mt5_service

router = APIRouter(tags=["mt5-onboarding"])
UserIdentity = Annotated[dict[str, Any], Depends(get_current_identity)]
OwnerIdentity = Annotated[dict[str, Any], Depends(require_permission("mt5_accounts.approve"))]


class SubmitMt5ApprovalRequest(BaseModel):
    login: str = Field(min_length=1, max_length=32)
    server: str = Field(min_length=2, max_length=160)


class ConnectApprovedMt5Request(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class MemberMt5OnboardingResponse(BaseModel):
    request_status: str
    request_login_masked: str | None
    request_server: str | None
    request_updated_at: datetime | None
    approved: bool
    approval_status: str
    approved_login_masked: str | None
    approved_server: str | None
    configured: bool
    connection_status: str
    remote_state: str | None
    remote_connection_status: str | None
    last_error_code: str | None
    last_checked_at: datetime | None
    last_connected_at: datetime | None


class PendingMt5ApprovalRequestResponse(BaseModel):
    request_id: UUID
    user_id: UUID
    email: str
    display_name: str | None
    login_masked: str
    server: str
    requested_at: datetime
    updated_at: datetime


class OwnerApproveRequestResponse(BaseModel):
    approved: bool
    user_id: UUID
    login_masked: str | None
    server: str | None
    status: str


def _connection_service(request: Request) -> Day30Mt5ConnectionService:
    service = require_mt5_service(request)
    if not isinstance(service, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "mt5_user_linking_not_configured", "message": "MT5 account linking is not available yet."},
        )
    return service


def _onboarding_service(request: Request) -> Day35Mt5OnboardingService:
    return Day35Mt5OnboardingService(_connection_service(request))


def _ordinary_user(identity: dict[str, Any]) -> None:
    if identity.get("role") != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "mt5_user_route_only", "message": "This setup route is for invited users only."},
        )


def _owner(identity: dict[str, Any]) -> None:
    if identity.get("role") != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "owner_required", "message": "Only the Owner can approve a user's MT5 account."},
        )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _safe_error(exc: Mt5ConnectionError) -> HTTPException:
    messages = {
        "mt5_login_invalid": "Enter your Vantage MT5 account number.",
        "mt5_server_invalid": "Enter the exact Vantage MT5 server name.",
        "mt5_vantage_server_required": "Only a Vantage MT5 account can be used with Super Signals.",
        "mt5_demo_not_available": "Use your live Vantage MT5 account. Demo accounts are not available for members.",
        "mt5_user_not_eligible": "This account is not eligible for member MT5 setup.",
        "mt5_account_already_approved": "Your MT5 account is already approved.",
        "mt5_request_not_pending": "There is no pending MT5 approval request for this member.",
        "mt5_account_not_approved": "Your MT5 account has not been approved yet.",
        "mt5_password_invalid": "Enter your MT5 trading password.",
        "metaapi_e_auth": "Vantage rejected the MT5 trading password. Check the password and try again.",
        "metaapi_timeout": "MT5 connectivity is temporarily unavailable. Try again shortly.",
        "metaapi_unreachable": "MT5 connectivity is temporarily unavailable. Try again shortly.",
        "metaapi_temporarily_unavailable": "MT5 connectivity is temporarily unavailable. Try again shortly.",
        "metaapi_provisioning_timeout": "MT5 setup is still processing. Retry Connect; Super Signals will reuse the same account.",
        "metaapi_platform_token_not_configured": "Super Signals MT5 connectivity needs administrator recovery.",
        "broker_credential_decryption_failed": "Super Signals MT5 connectivity needs administrator recovery.",
    }
    transient = {
        "metaapi_timeout",
        "metaapi_unreachable",
        "metaapi_temporarily_unavailable",
        "metaapi_provisioning_timeout",
        "metaapi_platform_token_not_configured",
        "broker_credential_decryption_failed",
    }
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE if exc.code in transient else status.HTTP_400_BAD_REQUEST,
        detail={"code": exc.code, "message": messages.get(exc.code, "The MT5 setup action could not be completed.")},
    )


def _member_response(request: Request, user_id: UUID) -> MemberMt5OnboardingResponse:
    connection_service = _connection_service(request)
    onboarding_service = Day35Mt5OnboardingService(connection_service)
    request_view = onboarding_service.get_request(user_id)
    approval = connection_service.get_user_approval(user_id)
    connection = connection_service.get_user_status(user_id)
    return MemberMt5OnboardingResponse(
        request_status=request_view.status,
        request_login_masked=request_view.login_masked,
        request_server=request_view.server,
        request_updated_at=request_view.updated_at,
        approved=approval.approved,
        approval_status=approval.status,
        approved_login_masked=approval.login_masked,
        approved_server=approval.server,
        configured=connection.configured,
        connection_status=connection.status,
        remote_state=connection.remote_state,
        remote_connection_status=connection.remote_connection_status,
        last_error_code=connection.last_error_code,
        last_checked_at=connection.last_checked_at,
        last_connected_at=connection.last_connected_at,
    )


@router.get("/account/mt5/onboarding", response_model=MemberMt5OnboardingResponse)
async def member_mt5_onboarding_status(
    request: Request, response: Response, identity: UserIdentity
) -> MemberMt5OnboardingResponse:
    _ordinary_user(identity)
    _no_store(response)
    return _member_response(request, identity["id"])


@router.post("/account/mt5/onboarding/request", response_model=MemberMt5OnboardingResponse)
async def submit_member_mt5_request(
    payload: SubmitMt5ApprovalRequest,
    request: Request,
    response: Response,
    identity: UserIdentity,
) -> MemberMt5OnboardingResponse:
    _ordinary_user(identity)
    try:
        _onboarding_service(request).submit_request(
            user_id=identity["id"], login=payload.login, server=payload.server
        )
    except Mt5ConnectionError as exc:
        raise _safe_error(exc) from exc
    _no_store(response)
    return _member_response(request, identity["id"])


@router.post("/account/mt5/onboarding/connect", response_model=MemberMt5OnboardingResponse)
async def connect_approved_member_mt5(
    payload: ConnectApprovedMt5Request,
    request: Request,
    response: Response,
    identity: UserIdentity,
) -> MemberMt5OnboardingResponse:
    _ordinary_user(identity)
    try:
        await _onboarding_service(request).connect_approved(
            user_id=identity["id"], password=payload.password
        )
    except Mt5ConnectionError as exc:
        raise _safe_error(exc) from exc
    _no_store(response)
    return _member_response(request, identity["id"])


@router.get("/owner/mt5/onboarding/requests", response_model=list[PendingMt5ApprovalRequestResponse])
async def list_member_mt5_requests(
    request: Request, response: Response, identity: OwnerIdentity
) -> list[PendingMt5ApprovalRequestResponse]:
    _owner(identity)
    rows = _onboarding_service(request).list_pending_requests()
    _no_store(response)
    return [PendingMt5ApprovalRequestResponse(**row.__dict__) for row in rows]


@router.post(
    "/owner/mt5/onboarding/requests/{user_id}/approve",
    response_model=OwnerApproveRequestResponse,
)
async def approve_member_mt5_request(
    user_id: UUID,
    request: Request,
    response: Response,
    identity: OwnerIdentity,
) -> OwnerApproveRequestResponse:
    _owner(identity)
    try:
        approval = _onboarding_service(request).approve_request(
            approver_user_id=identity["id"], user_id=user_id
        )
    except Mt5ConnectionError as exc:
        raise _safe_error(exc) from exc
    _no_store(response)
    return OwnerApproveRequestResponse(
        approved=approval.approved,
        user_id=approval.user_id,
        login_masked=approval.login_masked,
        server=approval.server,
        status=approval.status,
    )
