"""Select approved signals and let AIDY actually reason about each one's own geometry.

v1 scoped this to only `insufficient_track_record_evidence` approvals -- the case where
the deterministic engine has nothing else to say. That left every provider with an
established track record (the ones actually connected to real accounts) getting zero
signal-level reasoning, purely because their win rate alone was enough to clear the
deterministic bar. A provider with a good track record can still post an individual
signal with reckless geometry; the track record judges the provider, this judges the
signal. v2 covers every `approve` decision -- proven cheap (~$0.0005/call, the full
2,598-decision historical backlog costs about $1.30) and still bounded by the explicit
monthly budget gate below regardless.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_context_client import AidyCanonicalContext, AidyContextClient, AidyContextTerminalMiss
from app.aidy_market_client import AidyMarketClient
from app.aidy_reasoning_engine import (
    MODEL_VERSION,
    PROMPT_VERSION,
    AidyReasoningEngine,
    AidyReasoningUnavailable,
    SignalContext,
)
from app.aidy_reasoning_market_tools import build_candle_tool_executor
from app.provider_day19_explainer_budget import (
    ResourceBudget,
    ResourceUsage,
    evaluate_resource_budget,
)

logger = logging.getLogger(__name__)

_SELECTABLE = """
    SELECT d.id AS decision_id, d.decision_class, d.reasons, d.source_id,
           d.signal_posted_at,
           o.side, o.symbol, o.entry_low, o.entry_high, o.stop_loss, o.take_profits,
           COALESCE(NULLIF(s.chat_title, ''), s.source_alias) AS provider_name,
           COALESCE(b.trades_resolved, 0) AS trades_resolved,
           fp.summary AS provider_fingerprint_summary
    FROM aidy_decisions d
    JOIN provider_trade_observations o ON o.id = d.observation_id
    JOIN sources s ON s.id = d.source_id
    LEFT JOIN provider_trade_scoreboard b ON b.source_id = d.source_id
    LEFT JOIN aidy_reasoning_annotations a ON a.decision_id = d.id
    LEFT JOIN LATERAL (
        SELECT f.summary
        FROM provider_trade_fingerprints f
        WHERE f.source_id = d.source_id
        ORDER BY f.computed_at DESC
        LIMIT 1
    ) fp ON true
    WHERE d.decision_class = 'approve'
      AND a.id IS NULL
    ORDER BY d.decided_at
    LIMIT :limit
"""

_MONTHLY_USAGE_SQL = """
    SELECT count(*) AS calls, COALESCE(sum(estimated_cost_usd), 0) AS cost_usd
    FROM aidy_reasoning_annotations
    WHERE created_at >= date_trunc('month', now())
"""

_INSERT = """
    INSERT INTO aidy_reasoning_annotations (
        id, decision_id, lean, confidence, rationale, key_factors,
        model_version, prompt_version, model_name, response_id,
        input_tokens, output_tokens, estimated_cost_usd, latency_ms,
        market_context_available, request_count, tool_calls_made
    ) VALUES (
        :id, :decision_id, :lean, :confidence, :rationale, CAST(:key_factors AS jsonb),
        :model_version, :prompt_version, :model_name, :response_id,
        :input_tokens, :output_tokens, :estimated_cost_usd, :latency_ms,
        :market_context_available, :request_count, :tool_calls_made
    )
    ON CONFLICT (decision_id) DO NOTHING
"""


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _budget_from_environment() -> ResourceBudget:
    """Defaults sit inside the owner's pre-authorized ~EUR200/month new-spend ceiling."""
    soft_usd = float(os.getenv("AIDY_REASONING_SOFT_MONTHLY_USD", "120") or "120")
    hard_usd = float(os.getenv("AIDY_REASONING_HARD_MONTHLY_USD", "180") or "180")
    return ResourceBudget(
        soft_d1_reads=0,
        hard_d1_reads=1,
        soft_metaapi_calls=0,
        hard_metaapi_calls=1,
        soft_openai_calls=_positive_int("AIDY_REASONING_SOFT_MONTHLY_CALLS", 4000),
        hard_openai_calls=_positive_int("AIDY_REASONING_HARD_MONTHLY_CALLS", 6000),
        soft_cost_usd=soft_usd,
        hard_cost_usd=hard_usd,
    )


@dataclass
class ReasoningSummary:
    selected: int = 0
    written: int = 0
    skipped_budget: bool = False
    failed: int = 0
    by_lean: dict[str, int] = field(default_factory=dict)

    def record(self, lean: str) -> None:
        self.written += 1
        self.by_lean[lean] = self.by_lean.get(lean, 0) + 1

    def as_text(self) -> str:
        lines = [
            f"selected={self.selected}",
            f"written={self.written}",
            f"failed={self.failed}",
            f"skipped_budget={self.skipped_budget}",
        ]
        for name, count in sorted(self.by_lean.items(), key=lambda item: -item[1]):
            lines.append(f"  {name}={count}")
        return "\n".join(lines)


class AidyReasoningRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        engine: AidyReasoningEngine,
        budget: ResourceBudget | None = None,
        context_client: AidyContextClient | None = None,
        candle_client: AidyMarketClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine
        self._budget = budget or _budget_from_environment()
        self._context_client = context_client
        self._candle_client = candle_client

    @staticmethod
    def _market_context_summary(context: AidyCanonicalContext) -> dict:
        """Reduce AIDY's full context packet to what the reasoning prompt actually needs.

        The real packet carries digests, hashes and internal provenance the model has no
        use for and that would only burn tokens; this keeps the fields _SYSTEM_INSTRUCTIONS
        actually tells the model how to read.
        """
        regime = context.regime or {}
        labels = regime.get("labels") or {}
        trend_evidence = (regime.get("rule_evidence") or {}).get("trend_structure") or {}
        data_quality = context.data_quality or {}
        return {
            "as_of_utc": context.context_as_of_utc.isoformat(),
            "context_lag_seconds": context.context_lag_seconds,
            "session": labels.get("session"),
            "trend_structure": labels.get("trend_structure"),
            "trend_by_timeframe": trend_evidence.get("directions"),
            "volatility_band": labels.get("volatility_band"),
            "event_timing": labels.get("event_timing"),
            "quote_freshness": data_quality.get("quote_freshness"),
            "quote_state": data_quality.get("quote_state"),
        }

    async def _fetch_market_context(self, signal_posted_at) -> dict | None:
        """Best-effort only: a signal reasoned long after it posted has no live context left
        to fetch (AidyContextTerminalMiss(pit_context_stale)), and any other lookup failure
        must never block reasoning about the signal's own geometry -- fall back to None,
        exactly the no-context path this engine already had before market awareness existed.
        """
        if self._context_client is None:
            return None
        try:
            context = await self._context_client.fetch_context(as_of=signal_posted_at)
        except AidyContextTerminalMiss:
            return None
        except Exception:  # noqa: BLE001 - a context lookup must never fail the reasoning pass
            logger.warning(
                "AIDY market context lookup failed as_of=%s", signal_posted_at, exc_info=True
            )
            return None
        return self._market_context_summary(context)

    def _select(self, limit: int) -> list[dict]:
        with self._session_factory() as session:
            rows = session.execute(text(_SELECTABLE), {"limit": limit}).mappings().all()
        return [dict(row) for row in rows]

    def _monthly_usage(self) -> ResourceUsage:
        with self._session_factory() as session:
            row = session.execute(text(_MONTHLY_USAGE_SQL)).mappings().one()
        return ResourceUsage(
            openai_calls=int(row["calls"]),
            estimated_cost_usd=float(row["cost_usd"]),
        )

    def _persist(self, row: dict) -> bool:
        with self._session_factory() as session:
            result = session.execute(text(_INSERT), row)
            session.commit()
            return result.rowcount > 0

    async def run(self, *, limit: int = 100) -> ReasoningSummary:
        usage = await asyncio.to_thread(self._monthly_usage)
        budget_state = evaluate_resource_budget(usage=usage, budget=self._budget)
        summary = ReasoningSummary()
        if not budget_state["research_enrichment_allowed"]:
            summary.skipped_budget = True
            logger.warning(
                "AIDY reasoning monthly budget exhausted calls=%s cost_usd=%.2f",
                usage.openai_calls,
                usage.estimated_cost_usd,
            )
            return summary

        candidates = await asyncio.to_thread(self._select, limit)
        summary.selected = len(candidates)
        for candidate in candidates:
            market_context = await self._fetch_market_context(candidate["signal_posted_at"])
            context = SignalContext(
                decision_id=str(candidate["decision_id"]),
                provider_name=str(candidate["provider_name"]),
                side=str(candidate["side"]),
                symbol=str(candidate["symbol"]),
                entry_low=(
                    str(candidate["entry_low"]) if candidate["entry_low"] is not None else None
                ),
                entry_high=(
                    str(candidate["entry_high"]) if candidate["entry_high"] is not None else None
                ),
                stop_loss=(
                    str(candidate["stop_loss"]) if candidate["stop_loss"] is not None else None
                ),
                take_profits=list(candidate["take_profits"] or []),
                decision_class=str(candidate["decision_class"]),
                decision_reasons=list(candidate["reasons"] or []),
                trades_resolved=int(candidate["trades_resolved"]),
                provider_fingerprint_summary=(
                    str(candidate["provider_fingerprint_summary"])
                    if candidate["provider_fingerprint_summary"] is not None
                    else None
                ),
                market_context=market_context,
            )
            tool_executor = build_candle_tool_executor(
                self._candle_client, signal_posted_at=candidate["signal_posted_at"]
            )
            try:
                annotation = await self._engine.reason(context, tool_executor=tool_executor)
            except AidyReasoningUnavailable:
                summary.failed += 1
                logger.warning("AIDY reasoning call failed decision_id=%s", context.decision_id)
                continue

            row = {
                "id": uuid4(),
                "decision_id": candidate["decision_id"],
                "lean": annotation.lean,
                "confidence": annotation.confidence,
                "rationale": annotation.rationale,
                "key_factors": json.dumps(annotation.key_factors),
                "model_version": MODEL_VERSION,
                "prompt_version": PROMPT_VERSION,
                "model_name": annotation.model_name,
                "response_id": annotation.response_id,
                "input_tokens": annotation.input_tokens,
                "output_tokens": annotation.output_tokens,
                "estimated_cost_usd": annotation.estimated_cost_usd,
                "latency_ms": annotation.latency_ms,
                "market_context_available": market_context is not None,
                "request_count": annotation.request_count,
                "tool_calls_made": annotation.tool_calls_made,
            }
            if await asyncio.to_thread(self._persist, row):
                summary.record(annotation.lean)

            usage = ResourceUsage(
                openai_calls=usage.openai_calls + annotation.request_count,
                estimated_cost_usd=usage.estimated_cost_usd + float(annotation.estimated_cost_usd),
            )
            if not evaluate_resource_budget(usage=usage, budget=self._budget)[
                "research_enrichment_allowed"
            ]:
                summary.skipped_budget = True
                logger.warning(
                    "AIDY reasoning hit monthly budget mid-pass calls=%s cost_usd=%.2f",
                    usage.openai_calls,
                    usage.estimated_cost_usd,
                )
                break
        return summary


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database_url = os.getenv("DATABASE_URL", "").strip()
    api_key = os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("Open", "").strip()
    if not database_url:
        print("DATABASE_URL is required", flush=True)
        return 2
    if not api_key:
        print("OPENAI_API_KEY is required", flush=True)
        return 2

    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        runner = AidyReasoningRunner(
            sessionmaker(bind=engine, future=True),
            engine=AidyReasoningEngine(
                api_key=api_key,
                model=os.getenv("AIDY_REASONING_MODEL", "gpt-5-mini-2025-08-07").strip(),
            ),
            context_client=AidyContextClient.from_environment(),
            candle_client=AidyMarketClient.from_environment(),
        )
        summary = await runner.run(limit=args.limit)
    finally:
        engine.dispose()
    print(summary.as_text(), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(asyncio.run(_main()))


__all__ = ["AidyReasoningRunner", "ReasoningSummary"]
