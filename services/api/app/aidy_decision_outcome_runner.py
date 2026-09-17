"""Select every decided-but-unscored decision with a scorable trade, score it, persist.

Unlike ``aidy_decision_runner.py``, this never re-asks: ``aidy_decision_outcomes`` is
append-only and unique per decision, so a decision gets exactly one outcome row, ever.
A decision whose trade is still ``unresolvable`` or has no score row yet simply is not
selected -- it becomes eligible the moment ``provider_trade_scoring_runner.py`` gives it
a scorable outcome, whether that is this pass or a later one.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_decision_outcome_scorer import ScoreEvidence, row_params, score_decision

logger = logging.getLogger(__name__)

_SELECTABLE = """
    SELECT d.id AS decision_id, d.decision_class, s.outcome, s.net_pnl_usd, s.realized_r
    FROM aidy_decisions d
    JOIN provider_trade_scores s ON s.observation_id = d.observation_id
    LEFT JOIN aidy_decision_outcomes o ON o.decision_id = d.id
    WHERE o.id IS NULL
      AND s.outcome IN ('won', 'lost', 'breakeven', 'never_entered', 'open_at_window_end')
    ORDER BY d.decided_at
"""

_INSERT = """
    INSERT INTO aidy_decision_outcomes (
        id, decision_id, baseline_pnl_usd, baseline_realized_r,
        actual_pnl_usd, actual_realized_r, decision_delta_usd, resolved_at, resolution
    ) VALUES (
        :id, :decision_id, :baseline_pnl_usd, :baseline_realized_r,
        :actual_pnl_usd, :actual_realized_r, :decision_delta_usd, :resolved_at, :resolution
    )
    ON CONFLICT (decision_id) DO NOTHING
"""


@dataclass
class OutcomeSummary:
    selected: int = 0
    written: int = 0
    by_resolution: dict[str, int] = field(default_factory=dict)
    total_delta_usd: Decimal = Decimal(0)

    def record(self, resolution: str, delta_usd: Decimal) -> None:
        self.written += 1
        self.by_resolution[resolution] = self.by_resolution.get(resolution, 0) + 1
        self.total_delta_usd += delta_usd

    def as_text(self) -> str:
        lines = [
            f"selected={self.selected}",
            f"written={self.written}",
            f"total_delta_usd={self.total_delta_usd}",
        ]
        for name, count in sorted(self.by_resolution.items(), key=lambda item: -item[1]):
            lines.append(f"  {name}={count}")
        return "\n".join(lines)


class AidyDecisionOutcomeRunner:
    """Select scorable decisions, score them, persist -- append-only, write once."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

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

    async def run(self, *, limit: int | None = None, batch_size: int = 200) -> OutcomeSummary:
        candidates = await asyncio.to_thread(self._select, limit)
        summary = OutcomeSummary(selected=len(candidates))
        if not candidates:
            return summary

        for start in range(0, len(candidates), batch_size):
            chunk = candidates[start : start + batch_size]
            rows = []
            for candidate in chunk:
                evidence = ScoreEvidence(
                    outcome=candidate["outcome"],
                    net_pnl_usd=candidate["net_pnl_usd"],
                    realized_r=candidate["realized_r"],
                )
                result = score_decision(
                    candidate["decision_id"], candidate["decision_class"], evidence
                )
                rows.append(row_params(result))
                summary.record(result.resolution, result.decision_delta_usd)
            await asyncio.to_thread(self._persist, rows)
            logger.info("AIDY outcome scoring progress %s/%s", summary.written, summary.selected)
            print(f"scored outcomes {summary.written}/{summary.selected}", flush=True)
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
        runner = AidyDecisionOutcomeRunner(sessionmaker(bind=engine, future=True))
        summary = await runner.run(limit=args.limit, batch_size=args.batch_size)
    finally:
        engine.dispose()
    print(summary.as_text(), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(asyncio.run(_main()))


__all__ = ["AidyDecisionOutcomeRunner", "OutcomeSummary"]
