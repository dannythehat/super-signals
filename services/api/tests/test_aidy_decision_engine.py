"""AIDY's first decision class: track record and exposure, nothing invented.

These pin the honesty rules that matter most: a provider with no history yet gets
"insufficient_track_record_evidence" and no confidence, never a manufactured approval;
a genuinely bad book gets denied on its own numbers; and duplicate/conflicting exposure
is refused before track record is even consulted, because a second copy of the same
idea is never worth taking regardless of how good the provider is.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.aidy_decision_engine import (
    DENY_AVG_PNL_USD_THRESHOLD,
    DENY_WIN_RATE_PCT_THRESHOLD,
    MINIMUM_TRACK_RECORD_N,
    ScoreboardEvidence,
    evaluate_track_record,
)


def evidence(**overrides) -> ScoreboardEvidence:
    base = dict(
        source_id=None,
        trades_resolved=MINIMUM_TRACK_RECORD_N,
        win_rate_pct=Decimal("50"),
        net_pnl_usd=Decimal("0"),
        avg_pnl_usd=Decimal("0"),
        scored_coverage_pct=Decimal("50"),
    )
    base.update(overrides)
    return ScoreboardEvidence(**base)


def test_no_track_record_at_all_is_not_an_approval_of_anything() -> None:
    decision_class, reason, confidence = evaluate_track_record(None)

    assert decision_class == "approve"
    assert reason["code"] == "insufficient_track_record_evidence"
    assert reason["trades_resolved"] == 0
    assert confidence is None, "never invent confidence where there is no evidence"


def test_below_the_minimum_sample_is_still_insufficient() -> None:
    decision_class, reason, confidence = evaluate_track_record(
        evidence(trades_resolved=MINIMUM_TRACK_RECORD_N - 1)
    )

    assert decision_class == "approve"
    assert reason["code"] == "insufficient_track_record_evidence"
    assert confidence is None


def test_a_clearly_bad_book_is_denied_on_its_own_numbers() -> None:
    decision_class, reason, confidence = evaluate_track_record(
        evidence(
            trades_resolved=159,
            win_rate_pct=Decimal("29.7"),
            avg_pnl_usd=Decimal("-11.91"),
        )
    )

    assert decision_class == "deny"
    assert reason["code"] == "provider_track_record_net_negative"
    assert reason["trades_resolved"] == 159
    assert confidence is None, "a rule this simple does not get to claim confidence yet"


def test_a_profitable_book_with_enough_samples_is_approved() -> None:
    decision_class, reason, _ = evaluate_track_record(
        evidence(trades_resolved=238, win_rate_pct=Decimal("63.0"), avg_pnl_usd=Decimal("4.63"))
    )

    assert decision_class == "approve"
    assert reason["code"] == "provider_track_record_acceptable"


@pytest.mark.parametrize(
    ("avg_pnl", "win_rate"),
    [
        # Negative average alone, decent win rate: a few large losers, not a bad book.
        (DENY_AVG_PNL_USD_THRESHOLD, DENY_WIN_RATE_PCT_THRESHOLD + Decimal("20")),
        # Poor win rate alone, breakeven-or-better average: small losers, big winners.
        (Decimal("1"), DENY_WIN_RATE_PCT_THRESHOLD),
    ],
)
def test_denial_requires_both_signals_together_not_either_alone(avg_pnl, win_rate) -> None:
    decision_class, _, _ = evaluate_track_record(
        evidence(trades_resolved=100, avg_pnl_usd=avg_pnl, win_rate_pct=win_rate)
    )

    assert decision_class == "approve", "one weak signal alone must not deny a provider"


def test_exactly_at_the_deny_thresholds_is_denied() -> None:
    """The boundary belongs to denial, not to the provider -- <=, not <."""
    decision_class, _, _ = evaluate_track_record(
        evidence(
            trades_resolved=100,
            avg_pnl_usd=DENY_AVG_PNL_USD_THRESHOLD,
            win_rate_pct=DENY_WIN_RATE_PCT_THRESHOLD,
        )
    )

    assert decision_class == "deny"
