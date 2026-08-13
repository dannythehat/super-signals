"""Role-specific interface bootstrap routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.access_control import get_current_identity, require_permission
from app.permissions import ROLE_LABELS
from app.routes.control_centre_day35 import router as control_centre_day35_router

router = APIRouter(prefix="/access", tags=["access control"])
CurrentIdentity = Annotated[dict[str, Any], Depends(get_current_identity)]
OwnerUsers = Annotated[dict[str, Any], Depends(require_permission("users.manage"))]
OwnerSecurity = Annotated[dict[str, Any], Depends(require_permission("security.manage"))]
TradingSources = Annotated[dict[str, Any], Depends(require_permission("sources.manage"))]
TradingActivity = Annotated[dict[str, Any], Depends(require_permission("activity.view"))]
EmergencyStop = Annotated[dict[str, Any], Depends(require_permission("emergency_stop.use"))]
UserAccount = Annotated[dict[str, Any], Depends(require_permission("account.connect"))]
UserAutomation = Annotated[dict[str, Any], Depends(require_permission("automation.toggle"))]
UserPerformance = Annotated[dict[str, Any], Depends(require_permission("performance.view"))]


class CapabilityResponse(BaseModel):
    allowed: bool = True
    permission: str
    area: str
    role: str
    role_label: str


def _capability(identity: dict[str, Any], permission: str, area: str) -> CapabilityResponse:
    return CapabilityResponse(
        permission=permission,
        area=area,
        role=identity["role"],
        role_label=ROLE_LABELS.get(identity["role"], identity["role"]),
    )


@router.get("/me")
def access_me(identity: CurrentIdentity) -> dict[str, Any]:
    return {
        "role": identity["role"],
        "roles": identity["roles"],
        "permissions": identity["permissions"],
    }


@router.get("/owner/users", response_model=CapabilityResponse)
def owner_users(identity: OwnerUsers) -> CapabilityResponse:
    return _capability(identity, "users.manage", "owner")


@router.get("/owner/security", response_model=CapabilityResponse)
def owner_security(identity: OwnerSecurity) -> CapabilityResponse:
    return _capability(identity, "security.manage", "owner")


@router.get("/trading/sources", response_model=CapabilityResponse)
def trading_sources(identity: TradingSources) -> CapabilityResponse:
    return _capability(identity, "sources.manage", "trading")


@router.get("/trading/activity", response_model=CapabilityResponse)
def trading_activity(identity: TradingActivity) -> CapabilityResponse:
    return _capability(identity, "activity.view", "trading")


@router.get("/trading/emergency-stop", response_model=CapabilityResponse)
def emergency_stop_capability(identity: EmergencyStop) -> CapabilityResponse:
    return _capability(identity, "emergency_stop.use", "trading")


@router.get("/user/account", response_model=CapabilityResponse)
def user_account(identity: UserAccount) -> CapabilityResponse:
    return _capability(identity, "account.connect", "user")


@router.get("/user/automation", response_model=CapabilityResponse)
def user_automation(identity: UserAutomation) -> CapabilityResponse:
    return _capability(identity, "automation.toggle", "user")


@router.get("/user/performance", response_model=CapabilityResponse)
def user_performance(identity: UserPerformance) -> CapabilityResponse:
    return _capability(identity, "performance.view", "user")


router.include_router(control_centre_day35_router)
