from __future__ import annotations

from decimal import Decimal

from app.aidy_probability_ev_management import build_probability_ev_management_context


def _signal():
    return {
        "side": "BUY",
        "entry_low": "100",
        "entry_high": "100",
        "stop_loss": "90",
        "take_profits": ["110", "120", "130", "140"],
    }


def _build2():
    return {
        "broker_execution_calibration": {
            "status": "engineering_calibrated",
            "entry_adverse_p50_points": "0.5",
            "entry_adverse_p95_points": "1.0",
            "exit_adverse_p50_points": "0.5",
            "exit_adverse_p95_points": "1.5",
            "usd_per_point_per_lot_p50": "100",
            "cash_charge_per_lot_p50_usd": "10",
            "cash_charge_per_lot_p95_usd": "20",
        }
    }


def _build3(n=5):
    rows = []
    values = ["1.2", "-1.0", "0.8", "1.5", "-0.5"][:n]
    for idx, value in enumerate(values):
        rows.append(
            {
                "case_id": f"c{idx}",
                "prior_realized_r": value,
                "prior_pnl_usd": str(Decimal(value) * Decimal("10")),
            }
        )
    return {
        "historical_analogue": {
            "status": "descriptive_sample_available" if n >= 3 else "insufficient_prior_analogues",
            "analogues": rows,
            "selection_bias_possible": True,
            "descriptive_only": True,
            "usable_for_live_edge_claim": False,
        }
    }


def test_build4_geometry_probability_ev_and_management_are_research_only() -> None:
    result = build_probability_ev_management_context(
        signal=_signal(), build2_context=_build2(), build3_context=_build3()
    )
    assert result["signal_geometry"]["target_r_multiples"] == ["1", "2", "3", "4"]
    assert result["probability"]["sample_n"] == 5
    assert result["probability"]["status"] == "descriptive_low_sample"
    assert result["probability"]["usable_for_entry_override"] is False
    assert result["expected_value"]["status"] == "descriptive_empirical_analogue_ev"
    assert Decimal(result["expected_value"]["analogue_empirical_ev_r"]) == Decimal("0.4")
    assert result["expected_value"]["target_hit_probability_available"] is False
    assert result["expected_value"]["geometry_binary_ev_computed"] is False
    assert result["expected_value"]["usable_for_live_edge_claim"] is False
    assert result["management_profit_extraction"]["aidy_live_management_allowed"] is False
    assert result["management_profit_extraction"]["aidy_paper_management_allowed"] is False
    assert len(
        result["management_profit_extraction"]["existing_canonical_profit_protection_ladder"]
    ) == 2
    assert result["live_money_execution_allowed"] is False


def test_build4_execution_cost_proxy_converts_points_and_cash_to_r() -> None:
    result = build_probability_ev_management_context(
        signal=_signal(), build2_context=_build2(), build3_context=_build3()
    )
    # stop=10 points; p50 = 0.5 entry + 0.5 exit + $10/$100 = 1.1 points = 0.11R
    assert Decimal(
        result["execution_cost_proxy"]["estimated_execution_cost_r_p50"]
    ) == Decimal("0.11")


def test_build4_probability_and_ev_stay_unknown_below_five_resolved_analogues() -> None:
    result = build_probability_ev_management_context(
        signal=_signal(), build2_context=_build2(), build3_context=_build3(n=3)
    )
    assert result["probability"]["status"] == "insufficient_prior_outcomes"
    assert result["expected_value"]["status"] == "insufficient_evidence"
    assert result["expected_value"]["analogue_empirical_ev_r"] is None


def test_build4_does_not_invent_execution_cost_when_calibration_unknown() -> None:
    result = build_probability_ev_management_context(
        signal=_signal(),
        build2_context={"broker_execution_calibration": {"status": "insufficient_samples"}},
        build3_context=_build3(),
    )
    assert result["execution_cost_proxy"]["status"] == "unknown"
    assert result["expected_value"]["status"] == "descriptive_empirical_analogue_ev"
    assert result["expected_value"]["current_execution_cost_r_p50"] is None


def test_build4_management_reuses_day20_waiting_gate_and_canonical_ladder() -> None:
    result = build_probability_ev_management_context(
        signal=_signal(), build2_context=_build2(), build3_context=_build3()
    )
    management = result["management_profit_extraction"]
    assert management["day20_management_efficacy"] == "WAITING-FOR-FORWARD-EVIDENCE"
    assert management["research_management_posture"] == (
        "provider_baseline_only_until_forward_efficacy_is_proven"
    )
    assert management["canonical_ladder_authority_owner"] == "broker_settlement_canonical"
    assert management["aidy_must_not_duplicate_or_override_canonical_ladder"] is True
