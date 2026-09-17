"""Score each AIDY decision against the fixed baseline of what actually happened.

AIDY holds no live authority yet, so "what actually happened" and "the provider's own
unmodified plan" are the same real trade -- ``provider_trade_scores`` already has it,
replayed from price history by the existing scorer. This module does not re-derive that
number; it only asks, for each decision, what would have happened under AIDY's plan
instead, and writes the difference.

For a decision AIDY had no authority over, "AIDY's plan" is: an ``approve``/``continue``
changes nothing, so its outcome equals the baseline and the delta is always exactly zero
-- never a manufactured claim of help. A ``deny``/``conflict_deny``/``hold_no_second_entry``
means the trade is not taken under AIDY's plan, so its outcome is flat zero, and the delta
is the negative of whatever the real trade actually made or lost. A denied trade that
really lost money reads as ``confirmed_helped``; a denied trade that really won reads as
``confirmed_hurt``. Neither label is chosen by hand -- it falls directly out of the sign
of a real, already-computed number.

A trade the interpreter marked ``never_entered`` never risked anything either way, so its
baseline is zero and no decision about it can be credited or blamed. A trade still
``open_at_window_end`` in the scorer is scored with its current mark-to-market figures,
but flagged with that same resolution rather than a final one -- honest about not being
the last word, not a reason to wait indefinitely (``aidy_decision_outcomes`` is
append-only: this is the only row this decision will ever get).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

# Decision classes AIDY has no authority over today are scored two ways: an
# approve/continue changes nothing about what really happened, while a
# deny/conflict_deny/hold means the trade is treated as never taken.
UNCHANGED_CLASSES = frozenset({"approve", "continue"})
NOT_TAKEN_CLASSES = frozenset({"deny", "conflict_deny", "hold_no_second_entry"})

# provider_trade_scores outcomes this scorer can turn into a decision outcome.
# 'unresolvable' carries no pnl figure -- there is nothing to score yet.
SCORABLE_OUTCOMES = frozenset({"won", "lost", "breakeven", "never_entered", "open_at_window_end"})


@dataclass(frozen=True, slots=True)
class ScoreEvidence:
    outcome: str
    net_pnl_usd: Decimal | None
    realized_r: Decimal | None


@dataclass(frozen=True, slots=True)
class OutcomeResult:
    decision_id: UUID
    baseline_pnl_usd: Decimal
    baseline_realized_r: Decimal
    actual_pnl_usd: Decimal
    actual_realized_r: Decimal
    decision_delta_usd: Decimal
    resolution: str


def score_decision(
    decision_id: UUID, decision_class: str, evidence: ScoreEvidence
) -> OutcomeResult:
    """Score one decision against its already-computed baseline. Pure, no I/O."""
    baseline_pnl = evidence.net_pnl_usd if evidence.net_pnl_usd is not None else Decimal(0)
    baseline_r = evidence.realized_r if evidence.realized_r is not None else Decimal(0)

    if decision_class in NOT_TAKEN_CLASSES:
        actual_pnl, actual_r = Decimal(0), Decimal(0)
    else:
        actual_pnl, actual_r = baseline_pnl, baseline_r

    delta = actual_pnl - baseline_pnl

    if evidence.outcome == "open_at_window_end":
        resolution = "open_at_window_end"
    elif decision_class in NOT_TAKEN_CLASSES:
        if baseline_pnl > 0:
            resolution = "confirmed_hurt"
        elif baseline_pnl < 0:
            resolution = "confirmed_helped"
        else:
            resolution = "neutral"
    else:
        resolution = "neutral"

    return OutcomeResult(
        decision_id=decision_id,
        baseline_pnl_usd=baseline_pnl,
        baseline_realized_r=baseline_r,
        actual_pnl_usd=actual_pnl,
        actual_realized_r=actual_r,
        decision_delta_usd=delta,
        resolution=resolution,
    )


def row_params(result: OutcomeResult) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "decision_id": result.decision_id,
        "baseline_pnl_usd": result.baseline_pnl_usd,
        "baseline_realized_r": result.baseline_realized_r,
        "actual_pnl_usd": result.actual_pnl_usd,
        "actual_realized_r": result.actual_realized_r,
        "decision_delta_usd": result.decision_delta_usd,
        "resolved_at": datetime.now(UTC),
        "resolution": result.resolution,
    }


__all__ = [
    "NOT_TAKEN_CLASSES",
    "SCORABLE_OUTCOMES",
    "UNCHANGED_CLASSES",
    "OutcomeResult",
    "ScoreEvidence",
    "row_params",
    "score_decision",
]
