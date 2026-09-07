from pathlib import Path

from app.provider_day14_governance import (
    LIVE_GATE,
    OWNER_APPROVED_THRESHOLDS,
    STATISTICALLY_VALIDATED,
    decide_governance,
    simulated_governance_acceptance,
)

ROOT = Path(__file__).resolve().parents[1]


def test_waiting_forward_evidence_cannot_promote_even_with_candidate_counts() -> None:
    result = decide_governance(
        current_stage="shadow",
        research_profile_state="learning",
        source_statistical_status="WAITING-FOR-FORWARD-EVIDENCE",
        source_threshold_approval_status="PROPOSED_UNAPPROVED",
        positive_candidate_count=99,
        negative_candidate_count=0,
    )
    assert result.proposed_action == "HOLD"
    assert result.proposed_stage == "shadow"
    assert result.evidence_state == "WAITING_FORWARD_EVIDENCE"
    assert result.eligible_for_human_review is False


def test_unapproved_statistical_thresholds_are_fail_closed() -> None:
    result = decide_governance(
        current_stage="supervised",
        research_profile_state="learning",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status="PROPOSED_UNAPPROVED",
        positive_candidate_count=3,
        negative_candidate_count=0,
    )
    assert result.proposed_action == "HOLD"
    assert result.proposed_stage == "supervised"
    assert result.evidence_state == "WAITING_THRESHOLD_APPROVAL"


def test_validated_positive_evidence_moves_only_one_research_stage() -> None:
    result = decide_governance(
        current_stage="supervised",
        research_profile_state="learning",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=2,
        negative_candidate_count=0,
    )
    assert result.proposed_action == "PROMOTE"
    assert result.proposed_stage == "paper_candidate"
    assert result.eligible_for_human_review is False


def test_validated_adverse_evidence_demotes_one_stage_and_shadow_retests() -> None:
    demote = decide_governance(
        current_stage="paper_candidate",
        research_profile_state="qualified",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=0,
        negative_candidate_count=1,
    )
    assert demote.proposed_action == "DEMOTE"
    assert demote.proposed_stage == "supervised"

    retest = decide_governance(
        current_stage="shadow",
        research_profile_state="learning",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=0,
        negative_candidate_count=1,
    )
    assert retest.proposed_action == "RETEST"
    assert retest.proposed_stage == "shadow"


def test_duplicate_and_drift_states_never_promote() -> None:
    duplicate = decide_governance(
        current_stage="supervised",
        research_profile_state="duplicate_review",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=5,
        negative_candidate_count=0,
    )
    assert duplicate.proposed_action == "RETEST"
    assert duplicate.proposed_stage == "shadow"

    drift = decide_governance(
        current_stage="paper_candidate",
        research_profile_state="qualified",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=5,
        negative_candidate_count=0,
        drifted=True,
    )
    assert drift.proposed_action == "DEMOTE"
    assert drift.proposed_stage == "supervised"


def test_tiny_live_candidate_is_terminal_and_requires_separate_owner_gate() -> None:
    candidate = decide_governance(
        current_stage="paper_candidate",
        research_profile_state="qualified",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=3,
        negative_candidate_count=0,
    )
    assert candidate.proposed_stage == "tiny_live_candidate"
    assert candidate.human_gate_status == LIVE_GATE
    assert candidate.eligible_for_human_review is True

    terminal = decide_governance(
        current_stage="tiny_live_candidate",
        research_profile_state="qualified",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=10,
        negative_candidate_count=0,
    )
    assert terminal.proposed_action == "HOLD"
    assert terminal.proposed_stage == "tiny_live_candidate"
    assert terminal.human_gate_status == LIVE_GATE


def test_synthetic_governance_acceptance_proves_paths_without_provider_authority() -> None:
    result = simulated_governance_acceptance()
    assert result["synthetic_only"] is True
    assert result["acceptance_passed"] is True
    assert result["statistical_authority_granted"] is False
    assert result["live_money_authority_granted"] is False


def test_day14_source_is_shadow_only_and_broker_isolated() -> None:
    source = (ROOT / "app" / "provider_day14_governance.py").read_text(encoding="utf-8")
    lowered = source.casefold()
    assert "WHERE s.status='shadow'" in source
    assert "provider_conditional_runs" in source
    assert "provider_conditional_results" in source
    assert "provider_research_profiles" in source
    assert "UPDATE provider_governance_states" in source
    assert "UPDATE sources" not in source
    assert "broker_deals" not in source
    assert "from metaapi" not in lowered
    assert "import metaapi" not in lowered
    assert "aidy_market_client" not in source
    assert "httpx" not in source
    assert '"live_money_execution_allowed": False' in source


def test_day14_migration_hard_walls_research_state_from_live_authority() -> None:
    migration = (
        ROOT / "migrations" / "versions" / "0063_provider_day14_governance.py"
    ).read_text(encoding="utf-8")
    assert "tiny_live_candidate" in migration
    assert "authoritative_transition_count = 0" in migration
    assert "NOT authoritative_transition" in migration
    assert "ck_provider_governance_state_no_live_money" in migration
    assert "ck_provider_governance_run_no_live_money" in migration
    assert "ck_provider_governance_result_no_live_money" in migration
    assert "provider governance state is shadow-provider research only" in migration
    assert "provider governance result evidence is append-only" in migration


def test_day14_startup_keeps_day13_forward_refresh_and_runs_governance_after_it() -> None:
    startup = (ROOT.parents[1] / "scripts" / "render-start.sh").read_text(encoding="utf-8")
    assert "python -m app.provider_day13_runtime" in startup
    assert "python -m app.provider_day14_governance" in startup
    assert startup.index("python -m app.provider_day13_runtime") < startup.index("python -m app.provider_day14_governance")
