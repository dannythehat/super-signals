from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID\nfrom decimal import Decimal

import pytest

from app.aidy_provider_alpha_analogue import (
    build_conditional_alpha_context,
    build_historical_analogue_context,
)


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SOURCE = UUID("11111111-1111-1111-1111-111111111111")
OTHER = UUID("22222222-2222-2222-2222-222222222222")


def _payload(*, session="london", trend="bullish_trend", relation="inside_entry_zone"):
    return {
        "signal": {"side": "BUY"},
        "market_context": {
            "session": session,
            "trend_structure": trend,
            "volatility_band": "normal",
            "event_timing": "clear_current_window",
            "quote_freshness": "fresh",
            "quote_state": "known",
            "market": {"quote_context": {"spread": "0.20"}},
        },
        "event_liquidity_execution_context": {
            "execution_geometry": {
                "entry_zone_relation_to_mid": relation,
                "targets_already_crossed_at_quote": 0,
            }
        },
    }


def test_day13_underpowered_post_entry_cells_stay_non_actionable() -> None:
    rows = [
        {
            "model_version": "provider_day13_v1",
            "registry_version": "provider_day13_preregistered_v1",
            "evidence_cutoff": NOW - timedelta(minutes=1),
            "engineering_status": "ENGINEERING_PROVEN",
            "statistical_status": "WAITING-FOR-FORWARD-EVIDENCE",
            "threshold_approval_status": "PROPOSED_UNAPPROVED",
            "minimum_oos_n": 30,
            "regime_dimension": "trend_structure",
            "regime_value": "bullish_trend",
            "duration_bucket": "lt_15m",
            "duration_semantics": "realized_descriptive_post_entry",
            "cell_oos_n": 2,
            "complement_oos_n": 4,
            "p_value": None,
            "builder_gate_candidate": False,
            "authoritative_discovery": False,
        }
    ]
    result = build_conditional_alpha_context(
        rows, market_context=_payload()["market_context"], as_of=NOW
    )
    assert result["status"] == "available_but_non_actionable"
    assert result["usable_as_pretrade_alpha"] is False
    assert result["reason"] == "waiting_for_forward_evidence"
    assert result["max_matched_cell_oos_n"] == 2


def test_day13_future_run_is_rejected() -> None:
    rows = [
        {
            "evidence_cutoff": NOW + timedelta(seconds=1),
            "regime_dimension": "trend_structure",
            "regime_value": "bullish_trend",
        }
    ]
    with pytest.raises(ValueError, match="provider_alpha_future_evidence"):
        build_conditional_alpha_context(
            rows, market_context=_payload()["market_context"], as_of=NOW
        )


def test_historical_analogues_only_summarize_prior_resolved_similar_cases() -> None:
    rows = []
    for idx, pnl in enumerate(("10", "-5", "20", "1")):
        rows.append(
            {
                "case_id": f"case-{idx}",
                "source_id": SOURCE if idx < 2 else OTHER,
                "signal_posted_at": NOW - timedelta(days=idx + 2),
                "outcome_resolved_at": NOW - timedelta(days=idx + 1),
                "input_payload": _payload(),
                "actual_pnl_usd": pnl,
                "actual_realized_r": "0.5",
                "resolution": "won" if Decimal(pnl) > 0 else "lost",
            }
        )
    result = build_historical_analogue_context(
        target_payload=_payload(),
        target_source_id=SOURCE,
        target_signal_at=NOW,
        rows=rows,
    )
    assert result["status"] == "descriptive_sample_available"
    assert result["sample_n"] == 4
    assert result["positive_outcome_count"] == 3
    assert result["selection_bias_possible"] is True
    assert result["usable_for_live_edge_claim"] is False
    assert result["analogues"][0]["same_provider"] is True


def test_historical_analogue_rejects_outcome_resolved_after_target() -> None:
    with pytest.raises(ValueError, match="historical_analogue_future_evidence"):
        build_historical_analogue_context(
            target_payload=_payload(),
            target_source_id=SOURCE,
            target_signal_at=NOW,
            rows=[
                {
                    "case_id": "future",
                    "source_id": SOURCE,
                    "signal_posted_at": NOW - timedelta(hours=1),
                    "outcome_resolved_at": NOW + timedelta(seconds=1),
                    "input_payload": _payload(),
                    "actual_pnl_usd": "10",
                }
            ],
        )


def test_build3_source_binds_day13_and_analogue_pit_cutoffs() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "aidy_provider_alpha_analogue.py").read_text()
    assert "r.evidence_cutoff<=:as_of" in source
    assert "c.signal_posted_at<:as_of" in source
    assert "s.outcome_resolved_at<=:as_of" in source
    assert "aidy_historical_time_machine_v5" in source
    assert "aidy_historical_replay_input_v4" in source
