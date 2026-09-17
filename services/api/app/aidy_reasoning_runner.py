"""Select decisions AIDY's track record could not resolve, and let it actually reason.

Scoped deliberately narrow for v1: only `approve` decisions reasoned
`insufficient_track_record_evidence` -- the exact case where the deterministic engine
has nothing left to say and a real model read of the signal's own geometry adds
something the track record cannot yet. This also bounds cost naturally, on top of the
explicit monthly budget gate below.
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

from app.aidy_reasoning_engine import (
    MODEL_VERSION,
    PROMPT_VERSION,
    AidyReasoningEngine,
    AidyReasoningUnavailable,
    SignalContext,
)
from app.provider_day19_explainer_budget import (
    ResourceBudget,
    ResourceUsage,
    evaluate_resource_budget,
)

logger = logging.getLogger(__name__)

_SELECTABLE = """
    SELECT d.id AS decision_id, d.decision_class, d.reasons, d.source_id,
           o.side, o.symbol, o.entry_low, o.entry_high, o.stop_loss, o.take_profits,
           COALESCE(NULLIF(s.chat_title, ''), s.source_alias) AS provider_name
    FROM aidy_decisions d
    JOIN provider_trade_observations o ON o.id = d.observation_id
    JOIN sources s ON s.id = d.source_id
    LEFT JOIN aidy_reasoning_annotations a ON a.decision_id = d.id
    WHERE d.decision_class = 'approve'
      AND d.reasons @> '[{"code": "insufficient_track_record_evidence"}]'
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
        input_tokens, output_tokens, estimated_cost_usd, latency_ms
    ) VALUES (
        :id, :decision_id, :lean, :confidence, :rationale, CAST(:key_factors AS jsonb),
        :model_version, :prompt_version, :model_name, :response_id,
        :input_tokens, :output_tokens, :estimated_cost_usd, :latency_ms
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
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine
        self._budget = budget or _budget_from_environment()

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
                trades_resolved=0,
            )
            try:
                annotation = await asyncio.to_thread(self._engine.reason, context)
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
            }
            if await asyncio.to_thread(self._persist, row):
                summary.record(annotation.lean)

            usage = ResourceUsage(
                openai_calls=usage.openai_calls + 1,
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
        )
        summary = await runner.run(limit=args.limit)
    finally:
        engine.dispose()
    print(summary.as_text(), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(asyncio.run(_main()))


__all__ = ["AidyReasoningRunner", "ReasoningSummary"]
