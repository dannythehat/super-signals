"""Day 42 final GO / NO-GO contract."""

from app.day42_final_readiness import evaluate_day42_final_readiness
from app.routes.access import router as access_router


def _go_kwargs() -> dict[str, bool]:
    return {
        "day40_regression_proven": True,
        "day41_live_pilot_passed": True,
        "durable_database_verified": True,
        "monitoring_support_verified": True,
        "first_user_owner_approved": True,
        "ordinary_members_inactive": True,
        "global_emergency_absent": True,
        "publication_failures_clear": True,
        "owner_go_authorized": True,
    }


def test_day42_go_requires_every_gate_and_explicit_owner_authorization() -> None:
    result = evaluate_day42_final_readiness(**_go_kwargs())
    assert result.go is True
    assert result.ready_for_owner_go is True
    assert result.blockers == ()
    assert result.invitation_created is False
    assert result.trade_action_created is False


def test_day41_must_have_real_pass_evidence() -> None:
    kwargs = _go_kwargs()
    kwargs["day41_live_pilot_passed"] = False
    result = evaluate_day42_final_readiness(**kwargs)
    assert result.go is False
    assert result.prior_days_accepted is False
    assert "prior_days_not_fully_accepted" in result.blockers


def test_monitoring_and_durable_database_are_release_gates() -> None:
    kwargs = _go_kwargs()
    kwargs["durable_database_verified"] = False
    kwargs["monitoring_support_verified"] = False
    result = evaluate_day42_final_readiness(**kwargs)
    assert result.go is False
    assert "durable_database_not_verified" in result.blockers
    assert "monitoring_support_not_verified" in result.blockers


def test_first_user_cannot_be_green_without_owner_approval() -> None:
    kwargs = _go_kwargs()
    kwargs["first_user_owner_approved"] = False
    result = evaluate_day42_final_readiness(**kwargs)
    assert result.go is False
    assert "first_user_not_owner_approved" in result.blockers
    assert result.invitation_created is False


def test_existing_ordinary_member_automation_blocks_first_user_gate() -> None:
    kwargs = _go_kwargs()
    kwargs["ordinary_members_inactive"] = False
    result = evaluate_day42_final_readiness(**kwargs)
    assert result.go is False
    assert "ordinary_member_automation_already_active" in result.blockers


def test_publication_failure_blocks_go() -> None:
    kwargs = _go_kwargs()
    kwargs["publication_failures_clear"] = False
    result = evaluate_day42_final_readiness(**kwargs)
    assert result.go is False
    assert "publication_failures_present" in result.blockers


def test_owner_final_go_is_a_separate_last_decision() -> None:
    kwargs = _go_kwargs()
    kwargs["owner_go_authorized"] = False
    result = evaluate_day42_final_readiness(**kwargs)
    assert result.ready_for_owner_go is True
    assert result.go is False
    assert result.blockers == ("owner_final_go_not_authorized",)


def test_day42_endpoint_is_owner_read_only_surface() -> None:
    paths = {
        path
        for route in access_router.routes
        if (path := getattr(route, "path", None)) is not None
    }
    assert "/access/owner/day42-go-no-go" in paths
    assert not any("emergency-stop" in path or "emergency_stop" in path for path in paths)
