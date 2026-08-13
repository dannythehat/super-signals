"""Role-specific interface bootstrap routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.access_control import get_current_identity, require_permission
from app.day41_pilot_readiness import Day41PilotReadiness, read_day41_pilot_readiness
from app.day42_final_readiness import Day42FinalReadiness, read_day42_final_readiness
from app.permissions import ROLE_LABELS
from app.routes.control_centre_day35 import router as control_centre_day35_router

router = APIRouter(prefix="/access", tags=["access control"])
CurrentIdentity = Annotated[dict[str, Any], Depends(get_current_identity)]
OwnerUsers = Annotated[dict[str, Any], Depends(require_permission("users.manage"))]
OwnerSecurity = Annotated[dict[str, Any], Depends(require_permission("security.manage"))]
TradingSources = Annotated[dict[str, Any], Depends(require_permission("sources.manage"))]
TradingActivity = Annotated[dict[str, Any], Depends(require_permission("activity.view"))]
UserAccount = Annotated[dict[str, Any], Depends(require_permission("account.connect"))]
UserAutomation = Annotated[dict[str, Any], Depends(require_permission("automation.toggle"))]
UserPerformance = Annotated[dict[str, Any], Depends(require_permission("performance.view"))]


class CapabilityResponse(BaseModel):
    allowed: bool = True
    permission: str
    area: str
    role: str
    role_label: str


class Day41PilotReadinessResponse(BaseModel):
    ready: bool
    armed: bool
    blockers: tuple[str, ...]
    migration_ok: bool
    day40_regression_proven: bool
    canonical_owner_ok: bool
    owner_live_mt5_ok: bool
    owner_live_approval_ok: bool
    owner_risk_ok: bool
    owner_safe_start_ok: bool
    owner_mapped_exposure_clear: bool
    ordinary_members_inactive: bool
    global_emergency_absent: bool
    durable_database_verified: bool
    owner_limits_approved: bool
    trade_action_created: bool


class Day42FinalReadinessResponse(BaseModel):
    go: bool
    ready_for_owner_go: bool
    blockers: tuple[str, ...]
    day40_regression_proven: bool
    day41_live_pilot_passed: bool
    prior_days_accepted: bool
    durable_database_verified: bool
    monitoring_support_verified: bool
    first_user_owner_approved: bool
    ordinary_members_inactive: bool
    global_emergency_absent: bool
    publication_failures_clear: bool
    owner_go_authorized: bool
    invitation_created: bool
    trade_action_created: bool


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


@router.get("/owner/day41-pilot-readiness", response_model=Day41PilotReadinessResponse)
def day41_pilot_readiness(identity: OwnerSecurity) -> Day41PilotReadinessResponse:
    del identity
    readiness: Day41PilotReadiness = read_day41_pilot_readiness()
    return Day41PilotReadinessResponse(**readiness.as_dict())


@router.get("/owner/day42-go-no-go", response_model=Day42FinalReadinessResponse)
def day42_go_no_go(identity: OwnerSecurity) -> Day42FinalReadinessResponse:
    del identity
    readiness: Day42FinalReadiness = read_day42_final_readiness()
    return Day42FinalReadinessResponse(**readiness.as_dict())


@router.get("/trading/sources", response_model=CapabilityResponse)
def trading_sources(identity: TradingSources) -> CapabilityResponse:
    return _capability(identity, "sources.manage", "trading")


@router.get("/trading/activity", response_model=CapabilityResponse)
def trading_activity(identity: TradingActivity) -> CapabilityResponse:
    return _capability(identity, "activity.view", "trading")


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
