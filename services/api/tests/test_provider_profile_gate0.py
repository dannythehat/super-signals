from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.provider_profile_gate import (
    GATE_VERSION,
    evaluate_provider_profile_gate,
)
from app.provider_profile_pit import ProviderProfilePIT

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0070_provider_profile_gate0.py"


def _profile(*, accepted: int = 3, active_days: int = 3, observed: int = 30) -> ProviderProfilePIT:
    return ProviderProfilePIT(
        id=uuid4(),
        source_id=uuid4(),
        version_no=7,
        effective_at=datetime(2026, 9, 9, 10, 0, tzinfo=UTC),
        snapshot_fingerprint="a" * 64,
        source_identity={"source_alias": "Example Provider"},
        profile_snapshot={
            "style": "intraday",
            # Deliberately prove the gate does not trust a fuzzy readiness score.
            "interpretation_readiness": "0.99",
            "profile_metadata": {
                "adaptive_v1": {
                    "language": {
                        "accepted_signal_count": accepted,
                        "grammar_examples_masked": {
                            "new_trade": ["XAUUSD BUY <N> SL <N> TP <N>"],
                        },
                    }
                },
                "footprint_v1": {
                    "message_behaviour": {"observed_messages": observed},
                    "timing_fingerprint": {
                        "active_days": active_days,
                        "dominant_session_utc": "london",
                    },
                    "trade_geometry": {
                        "accepted_signals": accepted,
                        "median_stop_distance": 4.5,
                        "median_tp_count": 3.0,
                    },
                    "interpretation_context": {
                        "entry_style": "zone",
                        "order_style": "market",
                        "direction_style": "mixed",
                        "message_format": "structured_multiline",
                        "message_sequence": "reply_chain_management",
                        "dominant_session_utc": "london",
                        "management_style": "active_management",
                        "provider_vocabulary": ["secure", "runner", "breakeven"],
                    },
                },
            },
        },
    )


def test_complete_provider_specific_profile_qualifies() -> None:
    decision = evaluate_provider_profile_gate(_profile())
    assert decision.gate_version == GATE_VERSION
    assert decision.qualified is True
    assert decision.state == "qualified"
    assert decision.blockers == ()


def test_high_readiness_with_no_understood_trades_is_still_blocked() -> None:
    decision = evaluate_provider_profile_gate(_profile(accepted=0))
    assert decision.qualified is False
    assert decision.state == "profiling"
    assert "insufficient_understood_signals" in decision.blockers
    # A 0.99 readiness score must never override direct provider evidence.
    assert decision.evidence["accepted_signals"] == 0


def test_provider_needs_repeated_days_and_message_history() -> None:
    decision = evaluate_provider_profile_gate(_profile(active_days=1, observed=10))
    assert decision.qualified is False
    assert "insufficient_active_days" in decision.blockers
    assert "insufficient_message_history" in decision.blockers


def test_missing_pit_profile_fails_closed() -> None:
    decision = evaluate_provider_profile_gate(None)
    assert decision.qualified is False
    assert decision.blockers == ("provider_profile_pit_missing",)


def test_database_guard_uses_signal_time_profile_not_current_mutable_profile() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "provider_profile_gate_status" in source
    assert "provider_profile_gate0_snapshot_qualified" in source
    assert "provider_research_profile_versions" in source
    assert "id=NEW.provider_profile_version_id" in source
    assert "effective_at<=NEW.signal_posted_at" in source
    assert "provider_profile_gate_incomplete" in source
    assert "NEW.score_eligible := false" in source
    assert "NEW.aidy_score_blocked := true" in source


def test_gate_requires_concrete_provider_grammar_timing_and_geometry() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "accepted_signals >= 3",
        "active_days >= 3",
        "observed_messages >= 30",
        "entry_style <> 'unknown'",
        "order_style <> 'unknown'",
        "direction_style <> 'unknown'",
        "message_format <> 'unknown'",
        "message_sequence <> 'unknown'",
        "dominant_session_utc <> 'unknown'",
        "management_style <> 'unknown'",
        "median_stop_distance IS NOT NULL",
        "median_tp_count IS NOT NULL",
        "provider_vocabulary_count >= 3",
        "new_trade_example_count >= 1",
    ):
        assert required in source
