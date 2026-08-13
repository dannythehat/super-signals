"""Day 41 fail-closed Owner LIVE pilot readiness contract."""

from app.day41_pilot_readiness import evaluate_day41_pilot_readiness
from app.routes.access import router as access_router
from app.routes.admin_user_controls_day35 import router as user_controls_router


def _ready_kwargs() -> dict[str, bool]:
    return {
        "migration_ok": True,
        "day40_regression_proven": True,
        "canonical_owner_ok": True,
        "owner_live_mt5_ok": True,
        "owner_live_approval_ok": True,
        "owner_risk_ok": True,
        "owner_safe_start_ok": True,
        "owner_mapped_exposure_clear": True,
        "ordinary_members_inactive": True,
        "global_emergency_absent": True,
        "durable_database_verified": True,
        "owner_limits_approved": True,
        "pilot_armed": True,
    }


def test_day41_is_ready_only_when_every_gate_and_explicit_arm_are_true() -> None:
    result = evaluate_day41_pilot_readiness(**_ready_kwargs())
    assert result.ready is True
    assert result.armed is True
    assert result.blockers == ()
    assert result.trade_action_created is False


def test_demo_or_missing_live_account_blocks_pilot() -> None:
    kwargs = _ready_kwargs()
    kwargs["owner_live_mt5_ok"] = False
    result = evaluate_day41_pilot_readiness(**kwargs)
    assert result.ready is False
    assert result.armed is False
    assert "owner_live_mt5_not_connected" in result.blockers


def test_missing_owner_limits_and_durable_database_fail_closed() -> None:
    kwargs = _ready_kwargs()
    kwargs["durable_database_verified"] = False
    kwargs["owner_limits_approved"] = False
    result = evaluate_day41_pilot_readiness(**kwargs)
    assert result.ready is False
    assert "durable_database_not_verified" in result.blockers
    assert "owner_live_limits_not_approved" in result.blockers


def test_ordinary_member_automation_blocks_owner_only_pilot() -> None:
    kwargs = _ready_kwargs()
    kwargs["ordinary_members_inactive"] = False
    result = evaluate_day41_pilot_readiness(**kwargs)
    assert result.ready is False
    assert "ordinary_member_automation_active" in result.blockers


def test_owner_must_start_stopped_and_without_mapped_exposure() -> None:
    kwargs = _ready_kwargs()
    kwargs["owner_safe_start_ok"] = False
    kwargs["owner_mapped_exposure_clear"] = False
    result = evaluate_day41_pilot_readiness(**kwargs)
    assert result.ready is False
    assert "owner_automation_not_stopped_for_preflight" in result.blockers
    assert "owner_mapped_exposure_not_clear" in result.blockers


def test_global_emergency_control_presence_blocks_pilot() -> None:
    kwargs = _ready_kwargs()
    kwargs["global_emergency_absent"] = False
    result = evaluate_day41_pilot_readiness(**kwargs)
    assert result.ready is False
    assert "obsolete_global_emergency_control_present" in result.blockers


def test_explicit_arm_is_separate_from_technical_readiness() -> None:
    kwargs = _ready_kwargs()
    kwargs["pilot_armed"] = False
    result = evaluate_day41_pilot_readiness(**kwargs)
    assert result.ready is False
    assert result.armed is False
    assert result.blockers == ("owner_live_pilot_not_armed",)


def _registered_paths(router) -> set[str]:
    return {
        path
        for route in router.routes
        if (path := getattr(route, "path", None)) is not None
    }


def test_no_global_emergency_endpoint_exists_in_surviving_control_routers() -> None:
    paths = _registered_paths(access_router) | _registered_paths(user_controls_router)
    assert not any("emergency-stop" in path or "emergency_stop" in path for path in paths)
    assert "/access/owner/day41-pilot-readiness" in paths
    assert "/user-controls/users/{user_id}/revoke" in paths
