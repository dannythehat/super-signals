"""Outcome scoring pins the one number the owner actually asked for: did AIDY help.

An approve/continue never claims credit -- its outcome is defined to equal the
baseline, so its delta is always exactly zero. A deny/conflict_deny/hold is scored as
if the trade was never taken, so its delta is the negative of whatever the real trade
made or lost -- no avoided loss is claimed unless the real trade actually lost.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from app.aidy_decision_outcome_scorer import ScoreEvidence, score_decision


def test_approve_always_equals_the_baseline_and_claims_nothing() -> None:
    evidence = ScoreEvidence(
        outcome="lost", net_pnl_usd=Decimal("-42.00"), realized_r=Decimal("-1.0")
    )

    result = score_decision(uuid4(), "approve", evidence)

    assert result.baseline_pnl_usd == Decimal("-42.00")
    assert result.actual_pnl_usd == Decimal("-42.00")
    assert result.decision_delta_usd == Decimal("0")
    assert result.resolution == "neutral"


def test_a_denied_trade_that_really_lost_money_is_confirmed_helped() -> None:
    evidence = ScoreEvidence(
        outcome="lost", net_pnl_usd=Decimal("-30.00"), realized_r=Decimal("-1.0")
    )

    result = score_decision(uuid4(), "deny", evidence)

    assert result.actual_pnl_usd == Decimal("0")
    assert result.decision_delta_usd == Decimal("30.00")
    assert result.resolution == "confirmed_helped"


def test_a_denied_trade_that_really_won_is_confirmed_hurt() -> None:
    evidence = ScoreEvidence(
        outcome="won", net_pnl_usd=Decimal("55.00"), realized_r=Decimal("2.0")
    )

    result = score_decision(uuid4(), "conflict_deny", evidence)

    assert result.actual_pnl_usd == Decimal("0")
    assert result.decision_delta_usd == Decimal("-55.00")
    assert result.resolution == "confirmed_hurt"


def test_a_trade_that_never_entered_credits_no_one() -> None:
    evidence = ScoreEvidence(outcome="never_entered", net_pnl_usd=None, realized_r=None)

    result = score_decision(uuid4(), "hold_no_second_entry", evidence)

    assert result.baseline_pnl_usd == Decimal("0")
    assert result.decision_delta_usd == Decimal("0")
    assert result.resolution == "neutral"


def test_open_at_window_end_is_scored_but_flagged_provisional_not_final() -> None:
    evidence = ScoreEvidence(
        outcome="open_at_window_end", net_pnl_usd=Decimal("-12.00"), realized_r=Decimal("-0.4")
    )

    result = score_decision(uuid4(), "deny", evidence)

    assert result.decision_delta_usd == Decimal("12.00")
    assert result.resolution == "open_at_window_end", (
        "still owed a final answer, not confirmed_helped yet"
    )


def test_a_breakeven_denied_trade_is_neutral_not_helped_or_hurt() -> None:
    evidence = ScoreEvidence(outcome="breakeven", net_pnl_usd=Decimal("0"), realized_r=Decimal("0"))

    result = score_decision(uuid4(), "deny", evidence)

    assert result.decision_delta_usd == Decimal("0")
    assert result.resolution == "neutral"
