"""Provider forward-test execution gate.

Probation still protects member LIVE accounts: is_active_probation remains true until
an operator graduates a provider, so member distribution continues to exclude LIVE
accounts. The owner reference/demo account, however, must observe the provider's complete
forward behaviour. A probationary provider therefore executes both BUY and SELL signals
on the owner reference account instead of silently shadowing the historically weaker
side. This lets forward evidence, rather than a stale historical-side filter, decide
whether the provider should ultimately graduate.

A missing/invalid side is still blocked. Shadow-status providers remain fully isolated
from broker execution by the canonical dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ProbationCheck:
    eligible: bool
    reason: str


def is_active_probation(session: Session, *, source_id: UUID) -> bool:
    """True while a source is on probation and not yet graduated.

    Active probation prevents member LIVE distribution. It does not restrict the owner
    reference/demo account to only one side; both BUY and SELL are forward-tested.
    """
    row = session.execute(
        text("SELECT graduated FROM provider_execution_probation WHERE source_id = :source_id"),
        {"source_id": source_id},
    ).mappings().first()
    return row is not None and not bool(row["graduated"])


def check_probation_eligibility(
    session: Session, *, source_id: UUID, side: str | None
) -> ProbationCheck:
    probation = session.execute(
        text("SELECT graduated FROM provider_execution_probation WHERE source_id = :source_id"),
        {"source_id": source_id},
    ).mappings().first()
    if probation is None:
        return ProbationCheck(eligible=True, reason="not_probationary")
    if bool(probation["graduated"]):
        return ProbationCheck(eligible=True, reason="probation_graduated")

    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        return ProbationCheck(eligible=False, reason="probation_signal_side_unknown")

    return ProbationCheck(eligible=True, reason="probation_forward_test_all_sides")


__all__ = ["ProbationCheck", "check_probation_eligibility", "is_active_probation"]
