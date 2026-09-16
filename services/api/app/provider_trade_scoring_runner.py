"""Score every logical provider trade and keep the answer current as history arrives.

A score is derived, not observed. The same trade scores "no price history" today and
"lost $20" once the minutes behind it are backfilled, so this re-selects rows whose
answer can still change -- never scored, scored without history, or still open when the
follow window closed -- and leaves settled ones alone.

A Telegram message is one logical trade. Providers often build a signal by editing the
same post repeatedly (entry, then stop, then TP1, TP2, formatting, and so on). Historical
research must therefore score one canonical observation per message rather than treating
every revision as a fresh trade. The canonical database view chooses the *first complete
actionable revision* and exposes the time that revision actually existed. That prevents
both duplicate P&L and hindsight from replaying an edited trade from the original post
 timestamp.

Nothing here can reach a live decision. Scores are written to ``provider_trade_scores``,
whose ``forward_evidence_eligible`` column is CHECK-constrained false and which no
promotion gate reads, and the bars come from AIDY's research path, which refuses to
serve a response claiming point-in-time admissibility.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_market_client import AidyMarketClient
from app.provider_fairness import BENCHMARK_MODEL
from app.provider_trade_scorer import ProviderTradeScorer, TradeScore

logger = logging.getLogger(__name__)

# Version the repaired retrospective calculation separately from the first scoreboard
# generation. Existing contaminated rows remain auditable in provider_trade_scores, but
# the scoreboard and runner ignore them until the canonical observation is rescored.
RETROSPECTIVE_BENCHMARK_MODEL = f"{BENCHMARK_MODEL}_canonical_edits_v1"

# AIDY serves these from stored bars rather than the vendor, so the limit is politeness
# to a single Worker rather than a credit budget.
DEFAULT_CONCURRENCY = 4

# ``provider_trade_canonical_observations`` is created by migration 0093. It contains
# exactly one complete new-trade snapshot per Telegram message and aliases the timestamp
# at which that snapshot actually became available as ``observed_at``. Revision zero
# uses the original posted_at; edited revisions use message_revisions.edited_at with a
# conservative created_at fallback.
_SELECTABLE = """
    SELECT c.id, c.source_id, c.observed_at, c.side, c.entry_low, c.entry_high,
           c.stop_loss, c.take_profits, c.order_type
    FROM provider_trade_canonical_observations c
    LEFT JOIN provider_trade_scores s ON s.observation_id = c.id
    WHERE (
        s.id IS NULL
        -- A prior scoreboard generation used raw revision observations and the original
        -- message timestamp. Force every canonical trade through the repaired model.
        OR s.benchmark_model IS DISTINCT FROM :benchmark_model
        -- Re-ask only where more history could change the answer.
        OR s.outcome = 'open_at_window_end'
        OR s.unresolvable_reason = 'no_price_history_for_window'
        -- Blocked because the signal shared its minute bar with the seconds before it
        -- was posted. Replay now starts at the next whole minute, so these resolve;
        -- named one at a time rather than by prefix, because the other aidy_m1_
        -- blocks are genuinely undecidable and would be re-asked forever.
        OR s.unresolvable_reason = 'aidy_m1_signal_minute_ambiguous'
        -- Any fetch failure, not a list of exception names. An allowlist of the
        -- failures thought of in advance strands every trade that hit one that was
        -- not: 1,427 rows recorded research_fetch_failed:ValueError on the first run
        -- and would never have been re-asked once the cause was fixed.
        OR s.unresolvable_reason LIKE 'research_fetch_failed:%'
      )
    ORDER BY c.observed_at
"""

_UPSERT = """
    INSERT INTO provider_trade_scores (
        id, observation_id, source_id, scored_at, benchmark_model, entry_convention,
        outcome, unresolvable_reason, entry_price, net_pnl_usd, realized_r,
        legs_resolved, legs_total, first_bar_utc, last_bar_utc, bars_replayed,
        missing_minutes
    ) VALUES (
        :id, :observation_id, :source_id, now(), :benchmark_model, :entry_convention,
        :outcome, :unresolvable_reason, :entry_price, :net_pnl_usd, :realized_r,
        :legs_resolved, :legs_total, :first_bar_utc, :last_bar_utc, :bars_replayed,
        :missing_minutes
    )
    ON CONFLICT (observation_id) DO UPDATE SET
        scored_at = now(),
        benchmark_model = EXCLUDED.benchmark_model,
        entry_convention = EXCLUDED.entry_convention,
        outcome = EXCLUDED.outcome,
        unresolvable_reason = EXCLUDED.unresolvable_reason,
        entry_price = EXCLUDED.entry_price,
        net_pnl_usd = EXCLUDED.net_pnl_usd,
        realized_r = EXCLUDED.realized_r,
        legs_resolved = EXCLUDED.legs_resolved,
        legs_total = EXCLUDED.legs_total,
        first_bar_utc = EXCLUDED.first_bar_utc,
        last_bar_utc = EXCLUDED.last_bar_utc,
        bars_replayed = EXCLUDED.bars_replayed,
        missing_minutes = EXCLUDED.missing_minutes
"""


@dataclass
class ScoringSummary:
    selected: int = 0
    written: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)

    def record(self, score: TradeScore) -> None:
        self.written += 1
        self.outcomes[score.outcome] = self.outcomes.get(score.outcome, 0) + 1
        if score.unresolvable_reason:
            reason = score.unresolvable_reason
            self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_text(self) -> str:
        lines = [f"selected={self.selected}", f"written={self.written}"]
        for name, count in sorted(self.outcomes.items(), key=lambda item: -item[1]):
            lines.append(f"  {name}={count}")
        for name, count in sorted(self.reasons.items(), key=lambda item: -item[1]):
            lines.append(f"  reason:{name}={count}")
        return "\n".join(lines)


def _row_params(score: TradeScore) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "observation_id": score.observation_id,
        "source_id": score.source_id,
        "benchmark_model": RETROSPECTIVE_BENCHMARK_MODEL,
        "entry_convention": score.entry_convention,
        "outcome": score.outcome,
        "unresolvable_reason": score.unresolvable_reason,
        "entry_price": score.entry_price,
        "net_pnl_usd": score.net_pnl_usd,
        "realized_r": score.realized_r,
        "legs_resolved": score.legs_resolved,
        "legs_total": score.legs_total,
        "first_bar_utc": score.first_bar_utc,
        "last_bar_utc": score.last_bar_utc,
        "bars_replayed": score.bars_replayed,
        "missing_minutes": score.missing_minutes,
    }


class ProviderTradeScoringRunner:
    """Select the trades whose score can still change, rescore them, and persist."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: AidyMarketClient,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("provider_trade_scoring_concurrency_must_be_positive")
        self._session_factory = session_factory
        self._scorer = ProviderTradeScorer(client=client)
        self._concurrency = concurrency

    def _select(self, limit: int | None) -> list[dict[str, Any]]:
        sql = _SELECTABLE + ("\n    LIMIT :limit" if limit else "")
        params: dict[str, Any] = {"benchmark_model": RETROSPECTIVE_BENCHMARK_MODEL}
        if limit:
            params["limit"] = limit
        with self._session_factory() as session:
            rows = session.execute(text(sql), params).mappings().all()
        return [dict(row) for row in rows]

    def _persist(self, scores: list[TradeScore]) -> None:
        if not scores:
            return
        with self._session_factory() as session:
            session.execute(text(_UPSERT), [_row_params(score) for score in scores])
            session.commit()

    async def run(self, *, limit: int | None = None, batch_size: int = 200) -> ScoringSummary:
        observations = await asyncio.to_thread(self._select, limit)
        summary = ScoringSummary(selected=len(observations))
        if not observations:
            return summary

        gate = asyncio.Semaphore(self._concurrency)

        async def score_one(observation: dict[str, Any]) -> TradeScore:
            async with gate:
                return await self._scorer.score(observation)

        # Persisted in batches so a long run leaves usable results behind if it is
        # interrupted, rather than all or nothing.
        for start in range(0, len(observations), batch_size):
            chunk = observations[start : start + batch_size]
            scores = await asyncio.gather(*(score_one(item) for item in chunk))
            await asyncio.to_thread(self._persist, list(scores))
            for score in scores:
                summary.record(score)
            logger.info(
                "provider trade scoring progress %s/%s", summary.written, summary.selected
            )
            print(
                f"scored {summary.written}/{summary.selected}",
                flush=True,
            )
        return summary


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", flush=True)
        return 2
    client = AidyMarketClient.from_environment()
    if client is None:
        print(
            "AIDY_PROVIDER_MARKET_URL and AIDY_PROVIDER_MARKET_TOKEN are required",
            flush=True,
        )
        return 2

    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        runner = ProviderTradeScoringRunner(
            sessionmaker(bind=engine, future=True),
            client,
            concurrency=args.concurrency,
        )
        summary = await runner.run(limit=args.limit, batch_size=args.batch_size)
    finally:
        engine.dispose()
    print(summary.as_text(), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(asyncio.run(_main()))


__all__ = [
    "DEFAULT_CONCURRENCY",
    "ProviderTradeScoringRunner",
    "RETROSPECTIVE_BENCHMARK_MODEL",
    "ScoringSummary",
]
