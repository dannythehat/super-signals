from pathlib import Path

from app.provider_day15_monthly_audit import (
    CandidateGate,
    candidate_is_reviewable,
    pure_noise_month_acceptance,
)

ROOT = Path(__file__).resolve().parents[1]


def _all_pass() -> CandidateGate:
    return CandidateGate(
        sample_status="PASS",
        independence_status="PASS",
        ingress_status="PASS",
        interpretation_status="PASS",
        execution_cost_status="PASS",
        shadow_oos_status="POSITIVE_CONFIDENT",
        multiple_testing_status="PASS",
    )


def test_only_all_pass_candidate_is_reviewable() -> None:
    assert candidate_is_reviewable(_all_pass()) is True

    fields = (
        ("sample_status", "WAITING_FORWARD_EVIDENCE"),
        ("independence_status", "DEPENDENCE_REVIEW"),
        ("ingress_status", "UNVERIFIABLE_EXTERNAL_DENOMINATOR"),
        ("interpretation_status", "WAITING_THRESHOLD_APPROVAL"),
        ("execution_cost_status", "WAITING_FORWARD_EVIDENCE"),
        ("shadow_oos_status", "UNCERTAIN"),
        ("multiple_testing_status", "NO_BH_DISCOVERY"),
    )
    baseline = _all_pass().__dict__ if hasattr(_all_pass(), "__dict__") else {
        "sample_status": "PASS",
        "independence_status": "PASS",
        "ingress_status": "PASS",
        "interpretation_status": "PASS",
        "execution_cost_status": "PASS",
        "shadow_oos_status": "POSITIVE_CONFIDENT",
        "multiple_testing_status": "PASS",
    }
    for field, value in fields:
        values = dict(baseline)
        values[field] = value
        assert candidate_is_reviewable(CandidateGate(**values)) is False


def test_pure_noise_month_does_not_invent_star_provider() -> None:
    result = pure_noise_month_acceptance()
    assert result["synthetic_only"] is True
    assert result["provider_count"] == 40
    assert result["raw_top_mean_r"] > 0
    assert result["reviewable_candidate_count"] == 0
    assert result["acceptance_passed"] is True
    assert result["statistical_authority_granted"] is False
    assert result["live_money_authority_granted"] is False


def test_day15_source_uses_all_required_real_research_evidence_and_is_broker_isolated() -> None:
    source = (ROOT / "app" / "provider_day15_monthly_audit.py").read_text(encoding="utf-8")
    lowered = source.casefold()
    assert "WHERE s.status='shadow'" in source
    assert "provider_governance_runs" in source
    assert "provider_governance_results" in source
    assert "provider_conditional_results" in source
    assert "provider_research_profiles" in source
    assert "message_classifications" in source
    assert "message_revisions" in source
    assert "signal_lifecycle_events" in source
    assert "provider_shadow_execution_projection" in source
    assert "execution_adjusted_r_p95" in source
    assert "UNVERIFIABLE_EXTERNAL_DENOMINATOR" in source
    assert "rejection_reasons" in source
    assert "UPDATE sources" not in source
    assert "UPDATE provider_research_profiles" not in source
    assert "broker_deals" not in source
    assert "from metaapi" not in lowered
    assert "import metaapi" not in lowered
    assert "aidy_market_client" not in source
    assert "httpx" not in source
    assert '"live_money_execution_allowed": False' in source


def test_day15_migration_hard_walls_candidate_audit_from_live_authority() -> None:
    migration = (
        ROOT / "migrations" / "versions" / "0064_provider_day15_monthly_audit.py"
    ).read_text(encoding="utf-8")
    assert "provider_candidates" in migration
    assert "provider_monthly_audit_runs" in migration
    assert "provider_day15_policy_v1" in migration
    assert "UNSET_UNAPPROVED" in migration
    assert "real_promotion_authority_count = 0" in migration
    assert "real_demotion_authority_count = 0" in migration
    assert "NOT real_promotion_authority" in migration
    assert "NOT real_demotion_authority" in migration
    assert "ck_provider_candidate_no_live_money" in migration
    assert "provider candidate audit evidence is append-only" in migration


def test_day15_startup_runs_as_separate_fail_safe_research_sidecar() -> None:
    startup = (ROOT.parents[1] / "scripts" / "render-start.sh").read_text(encoding="utf-8")
    assert "python -m app.provider_day13_runtime" in startup
    assert "python -m app.provider_day14_governance" in startup
    assert "python -m app.provider_day15_monthly_audit" in startup
