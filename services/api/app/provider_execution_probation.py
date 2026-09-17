"""Should THIS signal from a probationary provider actually reach the broker?

A provider not in `provider_execution_probation` is unaffected -- this only restricts
sources the owner has explicitly placed on probation when switching them on. It never
sizes a trade and never overrides the deterministic decision engine; it only decides
whether an already-approved new_trade signal from a still-probationary provider matches
that provider's own already-computed best side (`provider_trade_fingerprints.best_side`,
computed daily from that provider's own resolved history). No side data yet, or the
fingerprint hasn't cleared its own honesty floor (`cohort_sample_met`) -- the signal is
held back, not guessed through. Held-back signals are still recorded exactly like a
shadow-status signal (observed, decided, scored) -- only the broker dispatch is skipped.
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

    if not side:
        return ProbationCheck(eligible=False, reason="probation_signal_side_unknown")

    fingerprint = session.execute(
        text(
            "SELECT cohort_sample_met, best_side FROM provider_trade_fingerprints "
            "WHERE source_id = :source_id ORDER BY computed_at DESC LIMIT 1"
        ),
        {"source_id": source_id},
    ).mappings().first()
    if fingerprint is None or not fingerprint["cohort_sample_met"] or not fingerprint["best_side"]:
        return ProbationCheck(eligible=False, reason="probation_insufficient_evidence")
    if str(fingerprint["best_side"]) != side:
        return ProbationCheck(
            eligible=False,
            reason=f"probation_side_not_historically_best:best={fingerprint['best_side']}",
        )
    return ProbationCheck(eligible=True, reason="probation_matches_best_side")


__all__ = ["ProbationCheck", "check_probation_eligibility"]
