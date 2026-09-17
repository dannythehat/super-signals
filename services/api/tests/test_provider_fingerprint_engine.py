"""The fingerprint summary must stay honest about what evidence actually supports."""

from __future__ import annotations

from decimal import Decimal

from app.provider_fingerprint_engine import _build_summary


def test_summary_states_geometry_when_sample_is_sufficient() -> None:
    summary = _build_summary(
        provider_name="Test Provider",
        trading_style="scalper",
        trades_resolved=50,
        win_rate_pct=Decimal("60.0"),
        avg_stop_won=Decimal("12.0"),
        avg_stop_lost=Decimal("8.0"),
        avg_rr_won=Decimal("2.1"),
        avg_rr_lost=Decimal("1.5"),
        geometry_sample_met=True,
        best_side="BUY",
        best_side_wr=Decimal("70.0"),
        worst_side="SELL",
        worst_side_wr=Decimal("40.0"),
        best_session="london",
        best_session_wr=Decimal("75.0"),
        worst_session="late",
        worst_session_wr=Decimal("30.0"),
        cohort_sample_met=True,
    )
    assert "Test Provider trades as a scalper" in summary
    assert "wider stop" in summary
    assert "2.10" in summary and "1.50" in summary
    assert "BUY" in summary and "SELL" in summary
    assert "london" in summary and "late" in summary
    assert "not a statistically certified rule" in summary.lower()


def test_summary_never_claims_geometry_below_the_sample_floor() -> None:
    summary = _build_summary(
        provider_name="Thin Provider",
        trading_style=None,
        trades_resolved=6,
        win_rate_pct=Decimal("50.0"),
        avg_stop_won=None,
        avg_stop_lost=None,
        avg_rr_won=None,
        avg_rr_lost=None,
        geometry_sample_met=False,
        best_side=None,
        best_side_wr=None,
        worst_side=None,
        worst_side_wr=None,
        best_session=None,
        best_session_wr=None,
        worst_session=None,
        worst_session_wr=None,
        cohort_sample_met=False,
    )
    assert "wider stop" not in summary
    assert "tighter stop" not in summary
    assert "Not enough resolved wins and losses" in summary
    assert "Not enough resolved trades in any single side/session slice" in summary


def test_summary_omits_style_sentence_when_style_is_unknown() -> None:
    summary = _build_summary(
        provider_name="Mystery Provider",
        trading_style="unknown",
        trades_resolved=20,
        win_rate_pct=Decimal("55.0"),
        avg_stop_won=Decimal("10.0"),
        avg_stop_lost=Decimal("10.0"),
        avg_rr_won=Decimal("1.8"),
        avg_rr_lost=Decimal("1.8"),
        geometry_sample_met=True,
        best_side=None,
        best_side_wr=None,
        worst_side=None,
        worst_side_wr=None,
        best_session=None,
        best_session_wr=None,
        worst_session=None,
        worst_session_wr=None,
        cohort_sample_met=False,
    )
    assert "trades as a unknown" not in summary
    assert "Mystery Provider" in summary
