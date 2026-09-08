from __future__ import annotations

import pytest

from app.provider_day17_confidence_sizing import (
    CalibrationObservation,
    HeatCaps,
    HeatLeg,
    PAPER_VARIABLE_SIZING_ALLOWED,
    VARIABLE_SIZING_AUTHORITY,
    allocate_research_heat,
    calibration_report,
    current_execution_size,
    engineering_acceptance_snapshot,
    proposed_envelope_risk_fraction,
    reliability_table,
    research_only_future_sizing_candidate,
    summarize_heat,
)


def test_reliability_table_builds_deterministic_equal_width_bins() -> None:
    rows = reliability_table(
        [
            CalibrationObservation(0.05, 0),
            CalibrationObservation(0.15, 0),
            CalibrationObservation(0.85, 1),
            CalibrationObservation(1.00, 1),
        ],
        bins=10,
    )

    assert len(rows) == 10
    assert rows[0]["n"] == 1
    assert rows[1]["n"] == 1
    assert rows[8]["n"] == 1
    assert rows[9]["n"] == 1
    assert rows[9]["confidence_upper"] == 1.0


def test_calibration_report_waits_for_forward_evidence() -> None:
    report = calibration_report(
        [
            CalibrationObservation(0.20, 0),
            CalibrationObservation(0.80, 1),
        ]
    )

    assert report["observation_n"] == 2
    assert report["evidence_status"] == "WAITING-FOR-FORWARD-EVIDENCE"
    assert report["variable_sizing_authority"] == "WAITING-FOR-FORWARD-EVIDENCE"
    assert report["paper_variable_sizing_allowed"] is False
    assert report["live_variable_sizing_allowed"] is False


def test_current_live_and_paper_size_is_confidence_invariant() -> None:
    for mode in ("paper", "live"):
        low = current_execution_size(
            accepted=True,
            flat_size_units=0.01,
            confidence=0.01,
            execution_mode=mode,
        )
        high = current_execution_size(
            accepted=True,
            flat_size_units=0.01,
            confidence=0.99,
            execution_mode=mode,
        )
        assert low["size_units"] == high["size_units"] == 0.01
        assert low["confidence_used_for_size"] is False
        assert high["confidence_used_for_size"] is False


def test_rejected_trade_is_zero_size_regardless_of_confidence() -> None:
    decision = current_execution_size(
        accepted=False,
        flat_size_units=0.01,
        confidence=1.0,
        execution_mode="paper",
    )

    assert decision["size_units"] == 0.0
    assert decision["sizing_method"] == "rejected_zero_size"


def test_future_envelope_math_is_bounded_and_non_executable() -> None:
    assert proposed_envelope_risk_fraction(0.0) == 0.002
    assert proposed_envelope_risk_fraction(1.0) == 0.020
    assert proposed_envelope_risk_fraction(0.5) == 0.011

    candidate = research_only_future_sizing_candidate(0.75)
    assert candidate["executable"] is False
    assert candidate["authority"] == "WAITING-FOR-FORWARD-EVIDENCE"
    assert 0.002 <= candidate["proposed_risk_fraction"] <= 0.020


def test_heat_summary_tracks_net_gross_and_clusters() -> None:
    summary = summarize_heat(
        [
            HeatLeg("BUY", 0.010, "cluster-a"),
            HeatLeg("BUY", 0.005, "cluster-a"),
            HeatLeg("SELL", 0.006, "cluster-b"),
        ]
    )

    assert summary["long_heat_fraction"] == 0.015
    assert summary["short_heat_fraction"] == 0.006
    assert summary["signed_net_heat_fraction"] == 0.009
    assert summary["absolute_net_heat_fraction"] == 0.009
    assert summary["gross_heat_fraction"] == 0.021
    assert summary["cluster_heat_fraction"] == {"cluster-a": 0.015, "cluster-b": 0.006}


def test_apparent_hedge_can_reduce_net_but_gross_cap_still_limits() -> None:
    result = allocate_research_heat(
        open_legs=[HeatLeg("BUY", 0.020, "cluster-a")],
        candidate=HeatLeg("SELL", 0.020, "cluster-b"),
        caps=HeatCaps(
            net_cap_fraction=0.030,
            gross_cap_fraction=0.025,
            cluster_cap_fraction=0.030,
        ),
    )

    assert result["allowed_risk_fraction"] == 0.005
    assert "gross_heat_cap" in result["limiting_reasons"]
    post = result["post_heat_if_research_allocation_applied"]
    assert post["gross_heat_fraction"] == 0.025
    assert post["absolute_net_heat_fraction"] == 0.015
    assert result["executable"] is False


def test_cluster_cap_prevents_copy_cluster_multiplication() -> None:
    result = allocate_research_heat(
        open_legs=[HeatLeg("BUY", 0.012, "relay-cluster")],
        candidate=HeatLeg("BUY", 0.010, "relay-cluster"),
        caps=HeatCaps(
            net_cap_fraction=0.030,
            gross_cap_fraction=0.040,
            cluster_cap_fraction=0.015,
        ),
    )

    assert result["allowed_risk_fraction"] == pytest.approx(0.003)
    assert "provider_cluster_heat_cap" in result["limiting_reasons"]


def test_net_directional_cap_limits_same_direction_candidate() -> None:
    result = allocate_research_heat(
        open_legs=[HeatLeg("BUY", 0.025, "cluster-a")],
        candidate=HeatLeg("BUY", 0.010, "cluster-b"),
        caps=HeatCaps(
            net_cap_fraction=0.030,
            gross_cap_fraction=0.050,
            cluster_cap_fraction=0.030,
        ),
    )

    assert result["allowed_risk_fraction"] == pytest.approx(0.005)
    assert "net_directional_heat_cap" in result["limiting_reasons"]


def test_existing_out_of_cap_book_fails_flat() -> None:
    result = allocate_research_heat(
        open_legs=[HeatLeg("BUY", 0.040, "cluster-a")],
        candidate=HeatLeg("SELL", 0.005, "cluster-b"),
        caps=HeatCaps(
            net_cap_fraction=0.030,
            gross_cap_fraction=0.050,
            cluster_cap_fraction=0.050,
        ),
    )

    assert result["allowed_risk_fraction"] == 0.0
    assert result["limiting_reasons"] == ["existing_heat_already_outside_caps"]


def test_invalid_calibration_inputs_fail_closed() -> None:
    with pytest.raises(ValueError, match="confidence_out_of_range"):
        calibration_report([CalibrationObservation(1.01, 1)])
    with pytest.raises(ValueError, match="realized_target_must_be_binary"):
        calibration_report([CalibrationObservation(0.5, 2)])


def test_day17_authority_is_dormant_by_construction() -> None:
    snapshot = engineering_acceptance_snapshot()

    assert VARIABLE_SIZING_AUTHORITY == "WAITING-FOR-FORWARD-EVIDENCE"
    assert PAPER_VARIABLE_SIZING_ALLOWED is False
    assert snapshot["engineering_harness_green"] is True
    assert snapshot["confidence_changes_current_paper_size"] is False
    assert snapshot["variable_sizing_authority"] == "WAITING-FOR-FORWARD-EVIDENCE"
