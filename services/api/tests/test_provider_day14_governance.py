from pathlib import Path

from app.provider_day14_governance import (
    OWNER_APPROVED,
    classify_performance,
    decide_governance,
    simulated_governance_acceptance,
)

ROOT = Path(__file__).resolve().parents[1]


def test_unapproved_policy_is_fail_closed() -> None:
    result = decide_governance(
        current_research_state="learning",
        policy_approval_status="PROPOSED_UNAPPROVED",
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    assert result.proposed_action == "HOLD"
    assert result.proposed_research_state == "learning"
    assert result.paper_qualified is False


def test_positive_evidence_advances_existing_research_states_one_step() -> None:
    learning = decide_governance(
        current_research_state="learning",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    assert learning.proposed_action == "PROMOTE"
    assert learning.proposed_research_state == "shadow"
    assert learning.paper_qualified is False

    shadow = decide_governance(
        current_research_state="shadow",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    assert shadow.proposed_action == "PROMOTE"
    assert shadow.proposed_research_state == "qualified"
    assert shadow.paper_qualified is True


def test_qualified_is_terminal_for_automatic_governance() -> None:
    result = decide_governance(
        current_research_state="qualified",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    assert result.proposed_action == "HOLD"
    assert result.proposed_research_state == "qualified"
    assert result.paper_qualified is True


def test_adverse_evidence_retests_before_sustained_demotion() -> None:
    first_bad = decide_governance(
        current_research_state="qualified",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="NEGATIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    assert first_bad.proposed_action == "RETEST"
    assert first_bad.proposed_research_state == "qualified"

    sustained = decide_governance(
        current_research_state="qualified",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="NEGATIVE_CONFIDENT",
        sustained_decay=True,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    assert sustained.proposed_action == "DEMOTE"
    assert sustained.proposed_research_state == "shadow"
    assert sustained.paper_qualified is False


def test_duplicate_review_and_same_evidence_never_promote() -> None:
    duplicate = decide_governance(
        current_research_state="duplicate_review",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=True,
        fresh_evidence_since_transition=True,
    )
    assert duplicate.proposed_action == "RETEST"
    assert duplicate.proposed_research_state == "duplicate_review"

    replayed = decide_governance(
        current_research_state="shadow",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=False,
    )
    assert replayed.proposed_action == "HOLD"
    assert replayed.proposed_research_state == "shadow"


def test_performance_gate_requires_minimum_forward_sample_and_tested_fingerprint() -> None:
    state, mean, lower, upper = classify_performance(
        [0.5] * 29,
        minimum_oos_n=30,
        tested_fingerprint_cells=1,
        minimum_tested_fingerprint_cells=1,
    )
    assert state == "INSUFFICIENT_OOS"
    assert mean == 0.5
    assert lower is None and upper is None

    state, _, _, _ = classify_performance(
        [0.5] * 30,
        minimum_oos_n=30,
        tested_fingerprint_cells=0,
        minimum_tested_fingerprint_cells=1,
    )
    assert state == "FINGERPRINT_NOT_READY"

    state, mean, lower, upper = classify_performance(
        [0.5] * 30,
        minimum_oos_n=30,
        tested_fingerprint_cells=1,
        minimum_tested_fingerprint_cells=1,
    )
    assert state == "POSITIVE_CONFIDENT"
    assert mean == 0.5
    assert lower is not None and lower > 0
    assert upper is not None and upper > 0


def test_synthetic_governance_acceptance_proves_paths_without_live_authority() -> None:
    result = simulated_governance_acceptance()
    assert result["synthetic_only"] is True
    assert result["acceptance_passed"] is True
    assert result["live_money_authority_granted"] is False


def test_day14_source_uses_shadow_profiles_and_is_broker_isolated() -> None:
    source = (ROOT / "app" / "provider_day14_governance.py").read_text(encoding="utf-8")
    lowered = source.casefold()
    assert "WHERE s.status='shadow'" in source
    assert "provider_conditional_runs" in source
    assert "provider_research_profiles" in source
    assert "UPDATE provider_research_profiles" in source
    assert "UPDATE sources" not in source
    assert "broker_deals" not in source
    assert "from metaapi" not in lowered
    assert "import metaapi" not in lowered
    assert "aidy_market_client" not in source
    assert "httpx" not in source
    assert '"live_money_execution_allowed": False' in source


def test_day14_migration_hard_walls_research_governance_from_live_authority() -> None:
    migration = (
        ROOT / "migrations" / "versions" / "0063_provider_day14_governance.py"
    ).read_text(encoding="utf-8")
    assert "provider_day14_policy_v1" in migration
    assert "PROPOSED_UNAPPROVED" in migration
    assert "OWNER_APPROVED" in migration
    assert "human_live_gate_required" in migration
    assert "authoritative_live_transition_count = 0" in migration
    assert "NOT authoritative_live_transition" in migration
    assert "ck_provider_governance_policy_no_live_money" in migration
    assert "ck_provider_governance_run_no_live_money" in migration
    assert "ck_provider_governance_result_no_live_money" in migration
    assert "provider governance result evidence is append-only" in migration


def test_research_scanner_preserves_governance_owned_states() -> None:
    scanner = (ROOT / "app" / "provider_research.py").read_text(encoding="utf-8")
    assert "provider_research_profiles.research_state IN ('shadow','qualified','rejected')" in scanner
    assert "THEN provider_research_profiles.research_state" in scanner


def test_day14_startup_keeps_day13_forward_refresh_and_runs_governance_after_it() -> None:
    startup = (ROOT.parents[1] / "scripts" / "render-start.sh").read_text(encoding="utf-8")
    assert "python -m app.provider_day13_runtime" in startup
    assert "python -m app.provider_day14_governance" in startup
    assert startup.index("python -m app.provider_day13_runtime") < startup.index("python -m app.provider_day14_governance")


def test_day14_uses_provider_specific_preregistration_boundaries() -> None:
    source = (ROOT / "app" / "provider_day14_governance.py").read_text(encoding="utf-8")
    assert "GROUP BY source_id" in source
    assert "day13_provider_preregistration_boundary_not_frozen" in source
    assert "frozen_boundaries.get(source_id)" in source
    assert '"frozen_oos_boundary": frozen_boundaries[source_id].isoformat()' in source
    assert "MIN(preregistered_at) AS first_boundary" in source
    assert "MAX(preregistered_at) AS last_boundary" in source
