from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.provider_intelligence_bf import (
    build_adaptation,
    build_book_conflict,
    build_fingerprint,
    build_governance,
    build_market_context,
)


def test_build_b_market_context_is_point_in_time_and_counts_context_coverage() -> None:
    rows = [
        {
            "signal_id": uuid4(),
            "session_json": {"name": "london"},
            "regime_json": {"name": "trend"},
            "closed_legs": 2,
            "benchmark_pnl_usd": 12.5,
        },
        {
            "signal_id": uuid4(),
            "session_json": {"name": "new_york"},
            "regime_json": {"name": "range"},
            "closed_legs": 1,
            "benchmark_pnl_usd": -3.0,
        },
    ]
    result = build_market_context(rows, accepted_signals=4)
    assert result["context_attached_signals"] == 2
    assert result["context_coverage"] == 0.5
    assert result["wins"] == 1
    assert result["losses"] == 1
    assert result["point_in_time_only"] is True


def test_build_c_and_e_reuse_sanitised_provider_learning_without_price_geometry() -> None:
    metadata = {
        "footprint_v1": {
            "profile_version": "provider-footprint-v1",
            "identity_fingerprint": {"behaviour_signature_sha256": "abc"},
            "interpretation_context": {
                "message_sequence": "edit_completed_setups",
                "dominant_session_utc": "london",
                "entry_style": "zone",
                "order_style": "market",
                "management_style": "active_management",
                "provider_vocabulary": ["secure", "runner"],
                "drift_status": "stable",
            },
        },
        "adaptive_v1": {
            "profile_version": "adaptive-provider-v1",
            "language": {
                "accepted_signal_count": 25,
                "cadence_bucket": "scalper",
                "sequence_bucket": "edit_completed_setup",
                "traits": {"uses_breakeven_language": True},
                "grammar_examples_masked": {"new_trade": ["BUY <N> SL <N> TP <N>"]},
            },
        },
    }
    fingerprint = build_fingerprint(metadata)
    adaptation = build_adaptation(fingerprint, metadata)
    assert fingerprint["behaviour_signature_sha256"] == "abc"
    assert adaptation["provider_specific"] is True
    assert adaptation["confidence"] == "high"
    assert adaptation["historical_numeric_levels_allowed"] is False
    assert adaptation["current_message_or_direct_reply_evidence_required"] is True
    assert adaptation["live_money_execution_allowed"] is False


def test_build_d_governance_can_flag_research_without_mutating_live_status() -> None:
    result = build_governance(
        source_status="live",
        research_state="shadow",
        observed_messages=100,
        accepted_signals=30,
        closed_shadow_legs=20,
        market_context={"context_coverage": 0.1},
        fingerprint={"drift_status": "stable"},
    )
    assert result["research_disposition"] == "quarantine_candidate"
    assert result["automatic_source_status_mutation_allowed"] is False
    assert result["source_status_mutation_performed"] is False
    assert result["live_money_execution_allowed"] is False


def test_build_f_combined_book_detects_conflict_but_never_nets_broker_orders() -> None:
    now = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
    buy_source = uuid4()
    sell_source = uuid4()
    result = build_book_conflict(
        [
            {"source_id": buy_source, "side": "BUY", "source_posted_at": now},
            {"source_id": sell_source, "side": "SELL", "source_posted_at": now},
        ],
        now=now,
    )
    assert result["provider_count"] == 2
    assert result["conflict_count"] == 1
    assert result["net_bias"] == "mixed"
    assert result["recommended_action"] == "observe_only"
    assert result["broker_netting_allowed"] is False
    assert result["live_money_execution_allowed"] is False
