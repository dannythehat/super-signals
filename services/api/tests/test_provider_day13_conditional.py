from pathlib import Path

import pytest

from app.provider_day13_conditional import (
    AIDY_REGIME_DEFINITION_VERSION,
    DURATION_BUCKETS,
    MINIMUM_OOS_N,
    PROPOSED_FDR_Q,
    PROPOSED_MIN_EFFECT_R,
    REGIME_VALUES,
    SESSIONS,
    SIDES,
    STATISTICAL_STATUS,
    THRESHOLD_APPROVAL_STATUS,
    _evaluate_values,
    _known_regime_labels,
    benjamini_hochberg,
    duration_bucket,
    simulated_governance_acceptance,
)

ROOT = Path(__file__).resolve().parents[1]


def test_duration_buckets_are_frozen_before_real_evaluation() -> None:
    assert duration_bucket(0) == "lt_15m"
    assert duration_bucket(899.999) == "lt_15m"
    assert duration_bucket(900) == "15m_to_60m"
    assert duration_bucket(3599.999) == "15m_to_60m"
    assert duration_bucket(3600) == "1h_to_4h"
    assert duration_bucket(14399.999) == "1h_to_4h"
    assert duration_bucket(14400) == "gte_4h"
    with pytest.raises(ValueError):
        duration_bucket(-1)


def test_benjamini_hochberg_is_deterministic_and_controls_global_family() -> None:
    result = benjamini_hochberg(
        {"a": 0.001, "b": 0.01, "c": 0.2, "d": 0.9}, q=0.05
    )
    assert result["a"] == {"adjusted_p": 0.004, "rejected": True}
    assert result["b"] == {"adjusted_p": 0.02, "rejected": True}
    assert result["c"]["rejected"] is False
    assert result["d"]["rejected"] is False


def test_known_signal_and_known_noise_simulations_behave_correctly() -> None:
    result = simulated_governance_acceptance()
    assert result["synthetic_only"] is True
    assert result["acceptance_passed"] is True
    assert "sim_000" in result["known_signal"]["candidate_keys"]
    assert result["pure_noise"]["builder_gate_candidate_count"] == 0
    assert result["statistical_authority_granted"] is False


def test_minimum_n_and_effect_rules_are_applied_before_candidate_status() -> None:
    below_n = _evaluate_values([1.0] * 29, [0.0] * 29)
    assert below_n["minimum_oos_gate_met"] is False
    assert below_n["p_value"] is None

    enough = _evaluate_values([1.0] * MINIMUM_OOS_N, [0.0] * MINIMUM_OOS_N)
    assert enough["minimum_oos_gate_met"] is True
    assert enough["minimum_effect_gate_met"] is True
    assert enough["p_value"] == 0.0
    assert abs(enough["shrunken_effect_r"]) >= PROPOSED_MIN_EFFECT_R


def test_unknown_or_noncanonical_regime_labels_are_not_confirmatory_evidence() -> None:
    packet = {
        "regime_definition_version": AIDY_REGIME_DEFINITION_VERSION,
        "labels": {
            "trend_structure": "unknown",
            "volatility_band": "normal",
            "quote_spread_condition": "unknown",
            "event_timing": "clear_current_window",
        },
    }
    assert _known_regime_labels(packet) == (
        ("volatility_band", "normal"),
        ("event_timing", "clear_current_window"),
    )
    assert _known_regime_labels({"regime_definition_version": "future_version", "labels": {}}) == ()


def test_preregistered_hypothesis_space_is_frozen_and_expected_size_for_40_shadow_providers() -> None:
    regime_values = sum(len(values) for values in REGIME_VALUES.values())
    assert regime_values == 12
    assert len(SIDES) == 2
    assert len(SESSIONS) == 4
    assert len(DURATION_BUCKETS) == 4
    assert 40 * len(SIDES) * len(SESSIONS) * len(DURATION_BUCKETS) * regime_values == 15360


def test_day13_thresholds_are_explicitly_proposed_not_owner_approved() -> None:
    assert MINIMUM_OOS_N == 30
    assert PROPOSED_FDR_Q == 0.05
    assert PROPOSED_MIN_EFFECT_R == 0.25
    assert THRESHOLD_APPROVAL_STATUS == "PROPOSED_UNAPPROVED"
    assert STATISTICAL_STATUS == "WAITING-FOR-FORWARD-EVIDENCE"


def test_day13_source_is_shadow_only_oos_pit_and_broker_isolated() -> None:
    source = (ROOT / "app" / "provider_day13_conditional.py").read_text(encoding="utf-8")
    assert "s.status='shadow'" in source
    assert "provider_signal_context_attachments" in source
    assert "t.score_eligible" in source
    assert "t.provider_profile_pit_status='resolved'" in source
    assert "a.context_as_of_utc<=a.signal_posted_at" in source
    assert "a.provider_profile_effective_at<=a.signal_posted_at" in source
    assert "observation.signal_posted_at <= hypothesis.preregistered_at" in source
    assert "broker_deals" not in source
    assert "MetaApi" not in source
    assert "metaapi" not in source.casefold()
    assert "aidy_market_client" not in source
    assert "httpx" not in source


def test_day13_migration_hard_walls_preregistration_and_authority() -> None:
    migration = (
        ROOT / "migrations" / "versions" / "0062_provider_day13_conditional.py"
    ).read_text(encoding="utf-8")
    assert "provider conditional preregistration is append-only" in migration
    assert "PROPOSED_UNAPPROVED" in migration
    assert "WAITING-FOR-FORWARD-EVIDENCE" in migration
    assert "authoritative_discovery_count = 0" in migration
    assert "NOT authoritative_discovery" in migration
    assert "ck_provider_conditional_run_no_live_money" in migration
    assert "ck_provider_conditional_result_no_live_money" in migration


def test_day13_replaces_completed_day12_startup_runner() -> None:
    startup = (ROOT.parents[1] / "scripts" / "render-start.sh").read_text(encoding="utf-8")
    assert "python -m app.provider_day13_runtime" in startup
    assert "python -m app.provider_day12_fingerprint" not in startup
