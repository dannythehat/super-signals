from __future__ import annotations

from pathlib import Path

from app.aidy_failure_self_critique import build_failure_self_critique_context


def _build2():
    return {
        "liquidity": {"quote_state": "known", "quote_freshness": "fresh"},
    }


def _build3():
    return {
        "conditional_alpha": {"usable_as_pretrade_alpha": False},
        "historical_analogue": {"status": "descriptive_sample_available"},
    }


def _build4():
    return {
        "signal_geometry": {
            "stop_distance_points": "10",
            "target_count": 3,
            "positive_target_r_count": 3,
        },
        "probability": {"status": "descriptive_low_sample"},
        "execution_cost_proxy": {"status": "engineering_calibrated_proxy"},
    }


def test_build5_flags_prior_over_reduction_without_turning_it_into_edge() -> None:
    result = build_failure_self_critique_context(
        market_context={"trend_structure": "mixed"},
        build2_context=_build2(),
        build3_context=_build3(),
        build4_context=_build4(),
        replay_self_feedback={
            "same_provider": {
                "status": "descriptive_prior_self_feedback",
                "sample_n": 8,
                "dominant_failure_mode": "over_reduction_of_profitable_trades",
                "guidance": "require_current_trade_specific_reason_before_reduce",
            }
        },
    )
    assert result["failure_attribution"]["selected_feedback_scope"] == "same_provider"
    assert result["risk_adjustment_guard"]["status"] == (
        "require_current_trade_specific_reason_before_reduce"
    )
    assert result["risk_adjustment_guard"]["reduce_requires_current_trade_specific_reason"] is True
    assert result["failure_attribution"]["usable_for_live_edge_claim"] is False
    assert result["live_money_execution_allowed"] is False


def test_build5_unknown_is_required_only_for_hard_geometry_failure() -> None:
    broken = _build4()
    broken["signal_geometry"] = {
        "stop_distance_points": "0",
        "target_count": 0,
        "positive_target_r_count": 0,
    }
    result = build_failure_self_critique_context(
        market_context={"trend_structure": "unknown"},
        build2_context={"liquidity": {"quote_state": "unknown", "quote_freshness": "unknown"}},
        build3_context={
            "conditional_alpha": {"usable_as_pretrade_alpha": False},
            "historical_analogue": {"status": "insufficient_prior_analogues"},
        },
        build4_context=broken,
    )
    assert result["unknown_gate"]["status"] == "unknown_required"
    assert "invalid_or_missing_stop_distance" in result["unknown_gate"]["critical_reasons"]
    assert "missing_take_profit_geometry" in result["unknown_gate"]["critical_reasons"]


def test_build5_many_soft_gaps_allow_unknown_but_do_not_force_it() -> None:
    result = build_failure_self_critique_context(
        market_context={"trend_structure": "unknown"},
        build2_context={"liquidity": {"quote_state": "unknown", "quote_freshness": "stale"}},
        build3_context={
            "conditional_alpha": {"usable_as_pretrade_alpha": False},
            "historical_analogue": {"status": "insufficient_prior_analogues"},
        },
        build4_context={
            **_build4(),
            "probability": {"status": "insufficient_prior_outcomes"},
            "execution_cost_proxy": {"status": "unknown"},
        },
    )
    assert result["unknown_gate"]["status"] == "unknown_permitted"
    assert result["unknown_gate"]["unknown_is_not_default_caution"] is True
    assert result["risk_adjustment_guard"]["uncertainty_alone_is_not_a_reduce_reason"] is True


def test_build5_insufficient_self_sample_makes_no_behavioural_adjustment() -> None:
    result = build_failure_self_critique_context(
        market_context={"trend_structure": "bullish_trend"},
        build2_context=_build2(),
        build3_context=_build3(),
        build4_context=_build4(),
        replay_self_feedback={
            "same_provider": {
                "status": "insufficient_prior_self_feedback",
                "sample_n": 2,
                "dominant_failure_mode": "insufficient_prior_self_feedback",
            },
            "global": {
                "status": "insufficient_prior_self_feedback",
                "sample_n": 4,
                "dominant_failure_mode": "insufficient_prior_self_feedback",
            },
        },
    )
    assert result["failure_attribution"]["selected_feedback_scope"] == "none"
    assert result["risk_adjustment_guard"]["status"] == "no_prior_failure_adjustment"
    assert result["research_only"] is True


def test_build5_replay_feedback_query_is_strictly_point_in_time() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app" / "aidy_failure_self_critique.py"
    ).read_text(encoding="utf-8")
    assert "c.signal_posted_at<:as_of" in source
    assert "s.outcome_resolved_at<:as_of" in source
    assert "d.replay_version=:replay_version" in source
    assert "c.input_contract_version=:input_contract_version" in source
    assert '_BUILD4_REPLAY_VERSION = "aidy_historical_time_machine_v7"' in source
    assert '_BUILD4_INPUT_CONTRACT_VERSION = "aidy_historical_replay_input_v6"' in source
