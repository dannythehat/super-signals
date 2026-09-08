from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.provider_day20_management_counterfactual import (
    ManagementDecision,
    SafetyState,
    detect_paper_live_divergence,
    engineering_acceptance_snapshot,
    management_authority_guard,
    management_effectiveness_report,
    resolve_management_counterfactual,
)


NOW = datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)


def test_management_decision_is_research_only_non_executable():
    d = ManagementDecision("s1", NOW, "protect", "volatility", protected_stop_r=0.0)
    d.validate()
    assert d.executable is False
    assert d.live_money_execution_allowed is False


def test_invalid_reduce_or_protect_geometry_fails_closed():
    with pytest.raises(ValueError, match="invalid_reduce_fraction"):
        ManagementDecision("s1", NOW, "reduce", "x", reduce_fraction=1.0).validate()
    with pytest.raises(ValueError, match="invalid_protected_stop"):
        ManagementDecision("s1", NOW, "protect", "x").validate()


def test_resolution_counts_saved_loss_and_sacrificed_winner():
    close = ManagementDecision("s1", NOW, "early_close", "research")
    saved = resolve_management_counterfactual(
        decision=close,
        untouched_provider_baseline_r=-1.0,
        intervention_gross_r=-0.2,
        intervention_execution_cost_r=0.05,
        resolved_at=NOW + timedelta(hours=1),
    )
    sacrificed = resolve_management_counterfactual(
        decision=close,
        untouched_provider_baseline_r=2.0,
        intervention_gross_r=0.4,
        intervention_execution_cost_r=0.1,
        resolved_at=NOW + timedelta(hours=1),
    )
    assert saved["management_delta_r"] == 0.75
    assert saved["loss_saved_r"] == 0.75
    assert sacrificed["winner_sacrificed_r"] == 1.7


def test_every_safety_blocker_forces_auto_revert_and_never_enables_management():
    fields = [
        "global_aidy_revoked",
        "kill_switch_active",
        "stale_inputs",
        "daily_loss_limit_breached",
        "max_drawdown_limit_breached",
        "consecutive_loss_limit_breached",
        "paper_live_divergence",
    ]
    for field in fields:
        result = management_authority_guard(SafetyState(**{field: True}))
        assert result["auto_revert_to_provider_baseline"] is True
        assert result["aidy_management_available"] is False
        assert result["live_management_allowed"] is False
        assert result["paper_management_allowed"] is False


def test_paper_live_divergence_requests_shadow_revert_without_live_mutation():
    result = detect_paper_live_divergence(
        shadow_action="protect",
        observed_live_action="hold",
        shadow_geometry_digest="a",
        live_geometry_digest="b",
    )
    assert result["divergence"] is True
    assert result["required_response"] == "AUTO_REVERT_SHADOW"
    assert result["live_mutation_created"] is False


def test_effectiveness_report_is_always_waiting_for_forward_evidence():
    d = ManagementDecision("s1", NOW, "hold", "baseline")
    row = resolve_management_counterfactual(
        decision=d,
        untouched_provider_baseline_r=1.0,
        intervention_gross_r=1.0,
        intervention_execution_cost_r=0.0,
        resolved_at=NOW,
    )
    report = management_effectiveness_report([row])
    assert report["engineering_status"] == "GREEN"
    assert report["management_efficacy"] == "WAITING-FOR-FORWARD-EVIDENCE"
    assert report["live_management_allowed"] is False


def test_day20_migration_is_chained_append_only_and_non_authoritative():
    text = Path("services/api/migrations/versions/0069_provider_day20_management.py").read_text()
    assert 'down_revision: str | None = "0068_provider_day19_usage"' in text
    assert "provider_day20_append_only" in text
    assert "BEFORE UPDATE OR DELETE" in text
    assert "CHECK (NOT live_money_execution_allowed)" in text
    assert "CHECK (NOT live_management_allowed)" in text


def test_engineering_snapshot_green_while_efficacy_waits():
    result = engineering_acceptance_snapshot()
    assert result["engineering_harness_green"] is True
    assert result["management_efficacy"] == "WAITING-FOR-FORWARD-EVIDENCE"
    assert result["live_management_allowed"] is False
    assert result["global_revoke_supported"] is True
