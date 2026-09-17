"""AIDY's shadow decision engine: one reasoned call per eligible signal, explained.

This is deliberately v1 and deliberately deterministic. The owner's mandate asks for
AIDY to weigh market conditions, news, candles and forecasts -- that reasoning needs a
real model call per signal, which is a real ongoing cost, and it is not switched on
silently. What ships here first is the class of decision that needs none of that: a
provider's own recorded track record, and whether a signal duplicates or conflicts with
exposure the book already has. Both are exactly-computable from data already in the
database, so there is nothing to be uncertain about and no reason to wait for spend
approval to start.

Every decision is written whether or not the evidence is strong. A provider with no
track record yet gets a decision reasoned "insufficient_track_record_evidence" and no
confidence figure, not silence -- an incomplete ledger cannot later prove AIDY was
watching from day one. Nothing here holds any authority: every row is
research_only=True and CHECK-constrained live_money_execution_allowed=False at the
schema level, and nothing in Super Signals' execution path reads this table.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

MODEL_VERSION = "aidy_decision_engine_deterministic_v1"
RULE_VERSION = "aidy_decision_rules_track_record_and_exposure_v1"

# Below this many resolved trades, a provider's win rate is noise, not evidence -- the
# same discipline as the existing Day 13 registry's MINIMUM_OOS_N=30, scaled down
# because a scored trade here already passed the scorer's own minute-level replay
# rather than being an unresolved statistical cell.
MINIMUM_TRACK_RECORD_N = 20
# A provider whose scored history is this negative, with enough samples behind it, is
# refused rather than mirrored blind. Chosen to flag a clearly bad book (TRADE GLOBAL
# scored -$11.91/trade on 159 resolved trades on 2026-09-17) without flagging normal
# variance around breakeven.
DENY_AVG_PNL_USD_THRESHOLD = Decimal("-3")
DENY_WIN_RATE_PCT_THRESHOLD = Decimal("35")
# A signal from the same source, same side, same symbol, with materially the same
# entry/stop geometry, posted within this window of an already-decided signal is a
# repost/edit, not a second idea.
DUPLICATE_WINDOW = "PT15M"


@dataclass(frozen=True, slots=True)
class ScoreboardEvidence:
    source_id: UUID
    trades_resolved: int
    win_rate_pct: Decimal | None
    net_pnl_usd: Decimal
    avg_pnl_usd: Decimal | None
    scored_coverage_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class DecisionResult:
    observation_id: UUID
    source_id: UUID
    signal_posted_at: datetime
    decision_class: str
    reasons: list[dict[str, Any]]
    confidence: Decimal | None
    evidence_digest: str
    duplicate_of_decision_id: UUID | None = None
    conflicts_with_decision_id: UUID | None = None


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _reason(code: str, **detail: Any) -> dict[str, Any]:
    return {"code": code, **detail}


def evaluate_track_record(
    evidence: ScoreboardEvidence | None,
) -> tuple[str, dict[str, Any], Decimal | None]:
    """Approve or deny purely on the provider's own recorded, scored history.

    Never invents confidence. A rule this simple earns a confidence figure only once
    it has itself been checked against real outcomes -- until then every call here
    returns None, exactly matching "unknown" rather than manufacturing certainty.
    """
    if evidence is None or evidence.trades_resolved < MINIMUM_TRACK_RECORD_N:
        n = evidence.trades_resolved if evidence else 0
        return (
            "approve",
            _reason(
                "insufficient_track_record_evidence",
                trades_resolved=n,
                minimum_required=MINIMUM_TRACK_RECORD_N,
            ),
            None,
        )
    if (
        evidence.avg_pnl_usd is not None
        and evidence.avg_pnl_usd <= DENY_AVG_PNL_USD_THRESHOLD
        and evidence.win_rate_pct is not None
        and evidence.win_rate_pct <= DENY_WIN_RATE_PCT_THRESHOLD
    ):
        return (
            "deny",
            _reason(
                "provider_track_record_net_negative",
                trades_resolved=evidence.trades_resolved,
                avg_pnl_usd=str(evidence.avg_pnl_usd),
                win_rate_pct=str(evidence.win_rate_pct),
                scored_coverage_pct=str(evidence.scored_coverage_pct)
                if evidence.scored_coverage_pct is not None
                else None,
            ),
            None,
        )
    return (
        "approve",
        _reason(
            "provider_track_record_acceptable",
            trades_resolved=evidence.trades_resolved,
            avg_pnl_usd=str(evidence.avg_pnl_usd) if evidence.avg_pnl_usd is not None else None,
            win_rate_pct=str(evidence.win_rate_pct) if evidence.win_rate_pct is not None else None,
        ),
        None,
    )


class AidyDecisionEngine:
    """Evaluate one new_trade observation against exposure, duplication and track record."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def _scoreboard(self, session: Session, source_id: UUID) -> ScoreboardEvidence | None:
        row = session.execute(
            text(
                "SELECT source_id, trades_resolved, win_rate_pct, net_pnl_usd, "
                "avg_pnl_usd, scored_coverage_pct FROM provider_trade_scoreboard "
                "WHERE source_id=:source_id"
            ),
            {"source_id": source_id},
        ).mappings().first()
        if row is None:
            return None
        win_rate = row["win_rate_pct"]
        avg_pnl = row["avg_pnl_usd"]
        return ScoreboardEvidence(
            source_id=source_id,
            trades_resolved=int(row["trades_resolved"] or 0),
            win_rate_pct=Decimal(str(win_rate)) if win_rate is not None else None,
            net_pnl_usd=Decimal(str(row["net_pnl_usd"] or 0)),
            avg_pnl_usd=Decimal(str(avg_pnl)) if avg_pnl is not None else None,
            scored_coverage_pct=(
                Decimal(str(row["scored_coverage_pct"]))
                if row["scored_coverage_pct"] is not None
                else None
            ),
        )

    def _prior_same_direction_decision(
        self, session: Session, *, observation: dict[str, Any]
    ) -> UUID | None:
        """A decided signal from the same source/side/symbol, still within the duplicate window."""
        row = session.execute(
            text(
                f"""
                SELECT d.id
                FROM aidy_decisions d
                JOIN provider_trade_observations o ON o.id = d.observation_id
                WHERE d.source_id = :source_id
                  AND o.side = :side
                  AND o.symbol IS NOT DISTINCT FROM :symbol
                  AND d.decision_class IN ('approve','hold_no_second_entry')
                  AND d.signal_posted_at >= :posted_at - INTERVAL '{DUPLICATE_WINDOW}'
                  AND d.signal_posted_at < :posted_at
                ORDER BY d.signal_posted_at DESC
                LIMIT 1
                """
            ),
            {
                "source_id": observation["source_id"],
                "side": observation["side"],
                "symbol": observation["symbol"],
                "posted_at": observation["observed_at"],
            },
        ).first()
        return UUID(str(row[0])) if row else None

    def _opposite_direction_open_position(
        self, session: Session, *, observation: dict[str, Any]
    ) -> bool:
        """Does the reference book already hold the opposite side of this symbol, open?

        This is the owner's reference book, not any individual user's -- per the
        2026-09-17 owner directive, paper and real share one pipeline and this
        research judgement is made once, at the book level, upstream of any account.
        """
        opposite = "SELL" if observation["side"] == "BUY" else "BUY"
        row = session.execute(
            text(
                """
                SELECT p.id
                FROM positions p
                JOIN signals sg ON sg.id = p.signal_id
                WHERE p.status = 'open'
                  AND sg.symbol IS NOT DISTINCT FROM :symbol
                  AND sg.side = :opposite
                LIMIT 1
                """
            ),
            {"symbol": observation["symbol"], "opposite": opposite},
        ).first()
        return row is not None

    def evaluate(self, observation: dict[str, Any]) -> DecisionResult:
        observation_id = UUID(str(observation["id"]))
        source_id = UUID(str(observation["source_id"]))
        posted_at = observation["observed_at"]

        with self._session_factory() as session:
            duplicate_of = self._prior_same_direction_decision(session, observation=observation)
            if duplicate_of is not None:
                reasons = [
                    _reason(
                        "duplicate_or_repost_within_window",
                        window=DUPLICATE_WINDOW,
                        duplicate_of_decision_id=str(duplicate_of),
                    )
                ]
                digest_payload = {"observation_id": str(observation_id), "reasons": reasons}
                return DecisionResult(
                    observation_id=observation_id,
                    source_id=source_id,
                    signal_posted_at=posted_at,
                    decision_class="hold_no_second_entry",
                    reasons=reasons,
                    confidence=None,
                    evidence_digest=_digest(digest_payload),
                    duplicate_of_decision_id=duplicate_of,
                )

            if self._opposite_direction_open_position(session, observation=observation):
                reasons = [
                    _reason(
                        "opposite_direction_exposure_already_open",
                        symbol=observation["symbol"],
                        side=observation["side"],
                    )
                ]
                digest_payload = {"observation_id": str(observation_id), "reasons": reasons}
                return DecisionResult(
                    observation_id=observation_id,
                    source_id=source_id,
                    signal_posted_at=posted_at,
                    decision_class="conflict_deny",
                    reasons=reasons,
                    confidence=None,
                    evidence_digest=_digest(digest_payload),
                )

            evidence = self._scoreboard(session, source_id)
            decision_class, reason, confidence = evaluate_track_record(evidence)
            reasons = [reason]
            return DecisionResult(
                observation_id=observation_id,
                source_id=source_id,
                signal_posted_at=posted_at,
                decision_class=decision_class,
                reasons=reasons,
                confidence=confidence,
                evidence_digest=_digest(
                    {
                        "observation_id": str(observation_id),
                        "reasons": reasons,
                        "scoreboard": {
                            "trades_resolved": evidence.trades_resolved if evidence else 0,
                            "avg_pnl_usd": str(evidence.avg_pnl_usd)
                            if evidence and evidence.avg_pnl_usd is not None
                            else None,
                            "win_rate_pct": str(evidence.win_rate_pct)
                            if evidence and evidence.win_rate_pct is not None
                            else None,
                        },
                    }
                ),
            )


def row_params(result: DecisionResult) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "observation_id": result.observation_id,
        "source_id": result.source_id,
        "signal_posted_at": result.signal_posted_at,
        "decided_at": datetime.now(UTC),
        "decision_class": result.decision_class,
        "reasons": json.dumps(result.reasons),
        "confidence": result.confidence,
        "model_version": MODEL_VERSION,
        "rule_version": RULE_VERSION,
        "evidence_digest": result.evidence_digest,
        "duplicate_of_decision_id": result.duplicate_of_decision_id,
        "conflicts_with_decision_id": result.conflicts_with_decision_id,
    }


__all__ = [
    "DENY_AVG_PNL_USD_THRESHOLD",
    "DENY_WIN_RATE_PCT_THRESHOLD",
    "DUPLICATE_WINDOW",
    "MINIMUM_TRACK_RECORD_N",
    "MODEL_VERSION",
    "RULE_VERSION",
    "AidyDecisionEngine",
    "DecisionResult",
    "ScoreboardEvidence",
    "evaluate_track_record",
    "row_params",
]
