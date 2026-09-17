"""Select every eligible signal without a decision yet, decide, and persist.

A decision is derived, like a score, so it stays re-runnable: a provider that crosses
the minimum-track-record threshold gets a real decision the moment enough evidence
exists, replacing the earlier "insufficient_track_record_evidence" call rather than
leaving it stranded. That is why this re-selects on rule_version rather than only on
"never decided" -- the same shape as provider_trade_scoring_runner.py's re-ask logic,
and for the same reason: evidence changes, so the answer has to be allowed to change
with it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_decision_engine import RULE_VERSION, AidyDecisionEngine, row_params

logger = logging.getLogger(__name__)

# A decision is worth making once the interpreter understood enough to describe a real
# trade: a side, an entry, a stop, at least one target -- the same bar
# provider_trade_scoring_runner.py uses, so every scored trade is also a decided one.
_SELECTABLE = f"""
    SELECT o.id, o.source_id, o.observed_at, o.side, o.symbol
    FROM provider_trade_observations o
    LEFT JOIN aidy_decisions d
        ON d.observation_id = o.id AND d.rule_version = '{RULE_VERSION}'
    WHERE o.decision = 'new_trade'
      AND o.side IS NOT NULL
      AND o.entry_low IS NOT NULL
      AND o.stop_loss IS NOT NULL
      AND jsonb_array_length(o.take_profits) > 0
      AND (
        d.id IS NULL
        -- Only re-ask a decision that was made on evidence too thin to be sure of.
        -- A resolved approve/deny/conflict/hold call does not get relitigated just
        -- because more trades were scored afterwards -- that would be exactly the
        -- hindsight rewriting the Decision Ledger exists to prevent.
        OR d.reasons @> '[{{"code": "insufficient_track_record_evidence"}}]'
      )
    ORDER BY o.observed_at
"""

_INSERT = """
    INSERT INTO aidy_decisions (
        id, observation_id, source_id, signal_posted_at, decided_at, decision_class,
        reasons, confidence, model_version, rule_version, evidence_digest,
        duplicate_of_decision_id, conflicts_with_decision_id
    ) VALUES (
        :id, :observation_id, :source_id, :signal_posted_at, :decided_at, :decision_class,
        CAST(:reasons AS jsonb), :confidence, :model_version, :rule_version, :evidence_digest,
        :duplicate_of_decision_id, :conflicts_with_decision_id
    )
    ON CONFLICT (observation_id, decision_class, rule_version) DO NOTHING
"""


@dataclass
class DecisionSummary:
    selected: int = 0
    written: int = 0
    by_class: dict[str, int] = field(default_factory=dict)

    def record(self, decision_class: str) -> None:
        self.written += 1
        self.by_class[decision_class] = self.by_class.get(decision_class, 0) + 1

    def as_text(self) -> str:
        lines = [f"selected={self.selected}", f"written={self.written}"]
        for name, count in sorted(self.by_class.items(), key=lambda item: -item[1]):
            lines.append(f"  {name}={count}")
        return "\n".join(lines)


class AidyDecisionRunner:
    """Select eligible observations, decide, and persist -- append-only, no rewrites."""

    def __init__(
        self, session_factory: sessionmaker[Session], *, engine: AidyDecisionEngine | None = None
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine or AidyDecisionEngine(session_factory)

    def _select(self, limit: int | None) -> list[dict]:
        sql = _SELECTABLE + ("\n    LIMIT :limit" if limit else "")
        with self._session_factory() as session:
            rows = session.execute(text(sql), {"limit": limit} if limit else {}).mappings().all()
        return [dict(row) for row in rows]

    def _persist(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self._session_factory() as session:
            session.execute(text(_INSERT), rows)
            session.commit()

    async def run(self, *, limit: int | None = None, batch_size: int = 200) -> DecisionSummary:
        observations = await asyncio.to_thread(self._select, limit)
        summary = DecisionSummary(selected=len(observations))
        if not observations:
            return summary

        for start in range(0, len(observations), batch_size):
            chunk = observations[start : start + batch_size]
            rows = []
            for observation in chunk:
                result = self._engine.evaluate(observation)
                rows.append(row_params(result))
                summary.record(result.decision_class)
            await asyncio.to_thread(self._persist, rows)
            logger.info("AIDY decisions progress %s/%s", summary.written, summary.selected)
            print(f"decided {summary.written}/{summary.selected}", flush=True)
        return summary


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", flush=True)
        return 2

    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        runner = AidyDecisionRunner(sessionmaker(bind=engine, future=True))
        summary = await runner.run(limit=args.limit, batch_size=args.batch_size)
    finally:
        engine.dispose()
    print(summary.as_text(), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(asyncio.run(_main()))


__all__ = ["AidyDecisionRunner", "DecisionSummary"]
