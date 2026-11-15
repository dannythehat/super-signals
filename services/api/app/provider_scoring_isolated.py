"""Isolated, research-only scorer for the entire provider portfolio.

Runs as a Render cron job. Does NOT start FastAPI, Telegram listeners, AIDY's
forward gate, MT5, broker clients or any live trading process. Only reads
recorded provider observations and retrospective AIDY candles, then writes
derived results to provider_trade_scores (forward_evidence_eligible=false).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_market_client import AidyMarketClient
from app.provider_trade_scoring_runner import ProviderTradeScoringRunner

logger = logging.getLogger(__name__)
SOFIA = ZoneInfo("Europe/Sofia")

# Include shadow/testing/paused sources whenever they posted a November trade.
# Exclude only independently established duplicates and test fixtures.
_MONTH_SELECT = text("""
    SELECT o.id, o.source_id, o.observed_at, o.side, o.entry_low,
           o.entry_high, o.stop_loss, o.take_profits, o.order_type
    FROM provider_trade_observations AS o
    JOIN sources AS src ON src.id = o.source_id
    LEFT JOIN provider_research_profiles AS profile
           ON profile.source_id = o.source_id
    LEFT JOIN provider_trade_scores AS score
           ON score.observation_id = o.id
    WHERE o.observed_at >= :period_start
      AND o.observed_at < :period_end
      AND o.decision = 'new_trade'
      AND o.side IS NOT NULL
      AND o.entry_low IS NOT NULL
      AND o.stop_loss IS NOT NULL
      AND jsonb_array_length(o.take_profits) > 0
      AND profile.duplicate_of_source_id IS NULL
      AND src.chat_title <> 'Test Signal Provider'
      AND (
          score.id IS NULL
          OR score.outcome = 'open_at_window_end'
          OR score.unresolvable_reason = 'no_price_history_for_window'
          OR score.unresolvable_reason = 'aidy_m1_signal_minute_ambiguous'
          OR score.unresolvable_reason LIKE 'research_fetch_failed:%'
      )
    ORDER BY
      CASE WHEN score.id IS NULL THEN 0 ELSE 1 END,
      row_number() OVER (PARTITION BY o.source_id ORDER BY o.observed_at),
      o.observed_at
    LIMIT :batch_limit
""")


def month_window(now: datetime) -> tuple[datetime, datetime]:
    """Use the owner's Bulgarian trading calendar, including DST."""
    month_start = datetime(now.year, now.month, 1, tzinfo=SOFIA)
    if month_start.month == 12:
        next_month = datetime(month_start.year + 1, 1, 1, tzinfo=SOFIA)
    else:
        next_month = datetime(month_start.year, month_start.month + 1, 1, tzinfo=SOFIA)
    return month_start.astimezone(UTC), next_month.astimezone(UTC)


def psycopg_url(raw: str) -> str:
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw[len("postgres://"):]
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw[len("postgresql://"):]
    if raw.startswith("postgresql+psycopg://"):
        return raw
    raise ValueError("database_connection_scheme_not_supported")


class MonthlyProviderRunner(ProviderTradeScoringRunner):
    """Target the current month and distribute the first results across providers."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: AidyMarketClient,
        *,
        period_start: datetime,
        period_end: datetime,
        concurrency: int = 2,
    ) -> None:
        super().__init__(session_factory, client, concurrency=concurrency)
        self._period_start = period_start
        self._period_end = period_end

    def _select(self, limit: int | None) -> list[dict[str, object]]:
        if limit is None or not 1 <= limit <= 100:
            raise ValueError("monthly_score_limit_must_be_between_1_and_100")
        with self._session_factory() as session:
            rows = session.execute(
                _MONTH_SELECT,
                {
                    "period_start": self._period_start,
                    "period_end": self._period_end,
                    "batch_limit": limit,
                },
            ).mappings().all()
        return [dict(row) for row in rows]


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not 1 <= args.limit <= 100 or not 1 <= args.concurrency <= 3:
        print("INVALID_BOUNDS: limit 1..100; concurrency 1..3", flush=True)
        return 2

    raw_url = os.environ.get("DATABASE_URL", "")
    market_url = os.environ.get("AIDY_PROVIDER_MARKET_URL", "")
    market_token = os.environ.get("AIDY_PROVIDER_MARKET_TOKEN", "")
    if not raw_url or not market_url or not market_token:
        print(
            "CONFIG_BLOCKED: DATABASE_URL / AIDY_PROVIDER_MARKET_URL / "
            "AIDY_PROVIDER_MARKET_TOKEN must be present (values withheld)",
            flush=True,
        )
        return 2

    period_start, period_end = month_window(datetime.now(SOFIA))
    client = AidyMarketClient(
        base_url=market_url,
        bearer_token=market_token,
        timeout_seconds=25,
    )
    # Fail closed before writing a single result if retrospective M1 is unavailable.
    # Choose a settled mid-session weekday window, not Sunday/weekend bars.
    probe_day = datetime.now(UTC) - timedelta(days=2)
    while probe_day.weekday() >= 5:
        probe_day -= timedelta(days=1)
    probe_start = probe_day.replace(hour=12, minute=0, second=0, microsecond=0)
    probe_end = probe_start + timedelta(minutes=10)
    try:
        market = await client.fetch_research_m1(start=probe_start, end=probe_end)
    except Exception as exc:
        print(f"RESEARCH_MARKET_PROBE_FAILED:{type(exc).__name__}", flush=True)
        return 3
    if not market.bars:
        print("RESEARCH_MARKET_NO_BARS: no scoring or database writes", flush=True)
        return 3

    engine = create_engine(
        psycopg_url(raw_url),
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        pool_recycle=120,
        pool_timeout=20,
        connect_args={
            "application_name": "super-signals-provider-monthly-research-only",
            "connect_timeout": 5,
            "options": (
                "-c statement_timeout=20000 "
                "-c lock_timeout=1000 "
                "-c idle_in_transaction_session_timeout=15000"
            ),
        },
        future=True,
    )
    try:
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        runner = MonthlyProviderRunner(
            session_factory, client,
            period_start=period_start,
            period_end=period_end,
            concurrency=args.concurrency,
        )
        if args.dry_run or os.environ.get("PROVIDER_RESEARCH_DRY_RUN", "") == "1":
            rows = await asyncio.to_thread(runner._select, args.limit)
            print(
                "DRY_RUN: period_start=%s selected=%d price_bars=%d writes=0"
                % (period_start.isoformat(), len(rows), len(market.bars)),
                flush=True,
            )
            return 0
        summary = await runner.run(limit=args.limit, batch_size=8)
        with session_factory() as session:
            result = session.execute(
                text("""
                    SELECT COUNT(*) AS n, COUNT(DISTINCT s.source_id) AS providers,
                           COUNT(*) FILTER (WHERE s.outcome IN
                             ('won','lost','breakeven')) AS resolved,
                           MAX(s.scored_at) AS last_scored
                    FROM provider_trade_scores s
                    JOIN provider_trade_observations o ON o.id=s.observation_id
                    WHERE o.observed_at >= :start_utc
                      AND o.observed_at < :end_utc
                """),
                {"start_utc": period_start, "end_utc": period_end},
            ).mappings().one()
        print(
            "MONTHLY_SCORING_RESULT selected=%d written=%d outcomes=%s "
            "month_score_rows=%s month_providers=%s resolved=%s last_scored=%s"
            % (
                summary.selected, summary.written, summary.outcomes,
                result["n"], result["providers"], result["resolved"],
                result["last_scored"],
            ),
            flush=True,
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
