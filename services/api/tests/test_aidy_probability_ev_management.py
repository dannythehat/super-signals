from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.aidy_probability_ev_management import build_probability_ev_management_context


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _claims(n=50, wins=30, losses=20):
    return [
        {
            "id": "provider.performance.side.BUY",
            "kind": "provider_side_performance",
            "sample_n": n,
            "as_of_utc": (NOW - timedelta(minutes=1)).isoformat(),
            "value": {"wins": wins, "losses": losses, "trades": n, "win_rate_percent": wins / n * 100},
        },
        {
            "id": "provider.performance.overall",
            "kind": "provider_performance",
            "sample_n": 200,
            "as_of_utc": (NOW - timedelta(minutes=1)).isoformat(),
            "value": {"wins": 100, "losses": 100, "closed_outcomes": 200, "win_rate_percent": 50},
        },
    ]


def _build2():
    return {
        "broker_execution_calibration": {
            "status": "engineering_calibrated",
            "entry_adverse_p50_points": "0.1",
            "exit_adverse_p50_points": "0.1",
            "entry_adverse_p95_points": "0.3",
            "exit_adverse_p95_points": "0.2",
        }
    }


def _build3():
    return {
        "historical_analogue": {
            "sample_n": 4,
            "positive_outcome_count": 3,
            "positive_outcome_rate": 0.75,
            "selection_bias_possible": True,
        }
    }


def test_build4_uses_specific_provider_cohort_not_model_confidence() -> None:
    result = build_probability_ev_management_context(
        signal_posted_at=NOW,
        side="BUY",
        entry_low="100",
        entry_high="100",
        stop_loss="90",
        take_profits=["120"],
        provider_evidence_claims=_claims(),
        provider_alpha_analogue_context=_build3(),
        event_liquidity_execution_context=_build2(),
        management_evidence={},
    )
    primary = result["probability"]["primary"]
    assert primary["selected_claim_id"] == "provider.performance.side.BUY"
    assert primary["positive_outcome_probability"] == 0.6
    assert result["probability"]["confidence_score_is_probability"] is False
    assert result["probability"]["secondary_analogue"]["positive_outcome_probability"] == 0.75
    assert result["probability"]["blend_status"].startswith("NOT_BLENDED")


def test_build4_ev_is_per_target_and_does_not_invent_tp_weights() -> None:
    result = build_probability_ev_management_context(
        signal_posted_at=NOW,
        side="BUY",
        entry_low="100",
        entry_high="100",
        stop_loss="90",
        take_profits=["120", "130"],
        provider_evidence_claims=_claims(),
        provider_alpha_analogue_context=_build3(),
        event_liquidity_execution_context=_build2(),
        management_evidence={},
    )
    ev = result["expected_value"]
    assert ev["status"] == "per_target_proxy_ev_available"
    assert ev["allocation_weighted_ev_r"] is None
    assert ev["allocation_weighted_ev_status"] == "UNKNOWN_NO_SAFE_TP_ALLOCATION_WEIGHTS"
    assert Decimal(ev["targets"][0]["reward_r"]) == Decimal("2")
    assert Decimal(ev["targets"][0]["break_even_probability"]) == Decimal("1") / Decimal("3")
    assert Decimal(ev["targets"][0]["net_ev_r_mid_p50_cost"]) == Decimal("0.78")


def test_build4_small_provider_sample_keeps_probability_and_ev_unknown() -> None:
    result = build_probability_ev_management_context(
        signal_posted_at=NOW,
        side="BUY",
        entry_low="100",
        entry_high="100",
        stop_loss="90",
        take_profits=["120"],
        provider_evidence_claims=_claims(n=10, wins=7, losses=3),
        provider_alpha_analogue_context={},
        event_liquidity_execution_context={},
        management_evidence={},
    )
    # side is too small, so the adequately sampled overall cohort is selected instead.
    assert result["probability"]["primary"]["selected_claim_id"] == "provider.performance.overall"
    assert result["probability"]["primary"]["sample_n"] == 200


def test_build4_management_keeps_existing_profit_protection_as_baseline_only() -> None:
    result = build_probability_ev_management_context(
        signal_posted_at=NOW,
        side="BUY",
        entry_low="100",
        entry_high="100",
        stop_loss="90",
        take_profits=["110", "120", "130", "140"],
        provider_evidence_claims=_claims(),
        provider_alpha_analogue_context=_build3(),
        event_liquidity_execution_context=_build2(),
        management_evidence={"decisions_n": 0, "outcomes_n": 0},
    )
    mgmt = result["profit_extraction_and_management"]
    assert mgmt["current_profit_protection_baseline"]["policy_changed_by_build4"] is False
    assert mgmt["current_profit_protection_baseline"]["remaining_leg_locked_floor_r_after_tp2"] == "0"
    assert Decimal(mgmt["current_profit_protection_baseline"]["remaining_leg_locked_floor_r_after_tp3"]) == Decimal("2")
    assert all(item["executable"] is False for item in mgmt["research_counterfactuals"])
    assert mgmt["live_management_allowed"] is False
    assert mgmt["paper_management_allowed"] is False


def test_build4_rejects_future_management_evidence() -> None:
    with pytest.raises(ValueError, match="management_evidence_from_future"):
        build_probability_ev_management_context(
            signal_posted_at=NOW,
            side="SELL",
            entry_low="100",
            entry_high="100",
            stop_loss="110",
            take_profits=["90"],
            provider_evidence_claims=_claims(),
            provider_alpha_analogue_context={},
            event_liquidity_execution_context={},
            management_evidence={"evidence_as_of_utc": NOW + timedelta(seconds=1)},
        )


def test_build4_source_has_strict_management_pit_cutoffs() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "aidy_probability_ev_management.py").read_text()
    assert "o.resolved_at<=:as_of" in source
    assert "d.decided_at<=:as_of" in source
    assert "LIVE_MANAGEMENT_ALLOWED" in source
    assert "LIVE_VARIABLE_SIZING_ALLOWED" in source
