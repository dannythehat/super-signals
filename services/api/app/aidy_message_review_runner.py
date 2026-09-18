"""Select recent skipped/ignored provider messages for AIDY's shadow second opinion."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_message_review_engine import (
    MODEL_VERSION,
    PROMPT_VERSION,
    AidyMessageReviewEngine,
    AidyMessageReviewUnavailable,
    MessageReviewContext,
)
from app.provider_day19_explainer_budget import ResourceBudget, ResourceUsage, evaluate_resource_budget

logger = logging.getLogger(__name__)

_SELECTABLE = """
    SELECT o.id AS observation_id,o.source_id,o.observed_at,o.decision,o.action,
           o.outcome_reason,left(m.raw_text,1600) AS current_message,
           COALESCE(NULLIF(s.source_alias,''),s.chat_title) AS provider_name,
           profile.version_no AS provider_profile_version_no,
           profile.profile_snapshot AS provider_profile_snapshot,
           recent.messages_json AS recent_messages
    FROM provider_trade_observations o
    JOIN messages m ON m.id=o.message_id
    JOIN sources s ON s.id=o.source_id
    LEFT JOIN aidy_message_reviews r ON r.observation_id=o.id
    LEFT JOIN LATERAL (
        SELECT v.version_no,v.profile_snapshot
        FROM provider_research_profile_versions v
        WHERE v.source_id=o.source_id
          AND v.effective_at<=o.observed_at
        ORDER BY v.effective_at DESC,v.version_no DESC
        LIMIT 1
    ) profile ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(
                   jsonb_build_object('posted_at',q.posted_at,'text',q.raw_text)
                   ORDER BY q.posted_at
               ) AS messages_json
        FROM (
            SELECT mm.posted_at,left(mm.raw_text,700) AS raw_text
            FROM messages mm
            WHERE mm.source_id=o.source_id
              AND mm.posted_at<=o.observed_at
            ORDER BY mm.posted_at DESC
            LIMIT 5
        ) q
    ) recent ON true
    WHERE r.id IS NULL
      AND s.status IN ('testing','shadow','live','active')
      AND o.observed_at>=now()-interval '7 days'
      AND (
        (
          o.decision='new_trade'
          AND o.action='skip'
          AND o.outcome_reason IN (
            'missing_instrument','missing_sl','missing_side','missing_entry',
            'literal_value_verification_failed','signal_entry_invalid',
            'strict_directional_validation_failed','pending_order_type_ambiguous',
            'unsupported_pending_order'
          )
        )
        OR (
          o.decision='trade_update'
          AND o.action='ignore'
          AND o.outcome_reason='unsupported_management'
        )
      )
    ORDER BY o.observed_at DESC
    LIMIT :limit
"""

_USAGE_SQL = """
    SELECT
      COALESCE((SELECT sum(request_count) FROM aidy_reasoning_annotations
                WHERE created_at>=date_trunc('month',now())),0)
      + COALESCE((SELECT count(*) FROM aidy_message_reviews
                  WHERE created_at>=date_trunc('month',now())),0) AS calls,
      COALESCE((SELECT sum(estimated_cost_usd) FROM aidy_reasoning_annotations
                WHERE created_at>=date_trunc('month',now())),0)
      + COALESCE((SELECT sum(estimated_cost_usd) FROM aidy_message_reviews
                  WHERE created_at>=date_trunc('month',now())),0) AS cost_usd
"""

_INSERT = """
    INSERT INTO aidy_message_reviews(
      id,observation_id,source_id,observed_at,original_decision,original_action,
      original_outcome_reason,review_class,suggested_action,confidence,missing_fields,
      rationale,suggested_parser_rule,provider_profile_version_no,model_version,
      prompt_version,model_name,response_id,input_tokens,output_tokens,
      estimated_cost_usd,latency_ms
    ) VALUES (
      :id,:observation_id,:source_id,:observed_at,:original_decision,:original_action,
      :original_outcome_reason,:review_class,:suggested_action,:confidence,
      CAST(:missing_fields AS jsonb),:rationale,:suggested_parser_rule,
      :provider_profile_version_no,:model_version,:prompt_version,:model_name,
      :response_id,:input_tokens,:output_tokens,:estimated_cost_usd,:latency_ms
    )
    ON CONFLICT (observation_id) DO NOTHING
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


def _budget() -> ResourceBudget:
    return ResourceBudget(
        soft_d1_reads=0,
        hard_d1_reads=1,
        soft_metaapi_calls=0,
        hard_metaapi_calls=1,
        soft_openai_calls=_positive_int("AIDY_REASONING_SOFT_MONTHLY_CALLS", 4000),
        hard_openai_calls=_positive_int("AIDY_REASONING_HARD_MONTHLY_CALLS", 6000),
        soft_cost_usd=float(os.getenv("AIDY_REASONING_SOFT_MONTHLY_USD", "120") or "120"),
        hard_cost_usd=float(os.getenv("AIDY_REASONING_HARD_MONTHLY_USD", "180") or "180"),
    )


@dataclass
class MessageReviewSummary:
    selected: int = 0
    written: int = 0
    failed: int = 0
    skipped_budget: bool = False
    by_class: dict[str, int] = field(default_factory=dict)

    def record(self, review_class: str) -> None:
        self.written += 1
        self.by_class[review_class] = self.by_class.get(review_class, 0) + 1


class AidyMessageReviewRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        engine: AidyMessageReviewEngine,
        budget: ResourceBudget | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine
        self._budget = budget or _budget()

    @staticmethod
    def _profile_summary(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        metadata = value.get("profile_metadata") if isinstance(value.get("profile_metadata"), dict) else {}
        adaptive = metadata.get("adaptive_v1") if isinstance(metadata.get("adaptive_v1"), dict) else {}
        footprint = metadata.get("footprint_v1") if isinstance(metadata.get("footprint_v1"), dict) else {}
        language = adaptive.get("language") if isinstance(adaptive.get("language"), dict) else {}
        interpretation = footprint.get("interpretation_context") if isinstance(footprint.get("interpretation_context"), dict) else {}
        return {
            "research_state": value.get("research_state"),
            "style": value.get("style"),
            "interpretation_readiness": value.get("interpretation_readiness"),
            "interpretation_context": interpretation,
            "sequence_bucket": language.get("sequence_bucket"),
            "cadence_bucket": language.get("cadence_bucket"),
            "traits": language.get("traits") or {},
            "grammar_examples_masked": language.get("grammar_examples_masked") or {},
        }

    def _select(self, limit: int) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.execute(text(_SELECTABLE), {"limit": limit}).mappings().all()
        return [dict(row) for row in rows]

    def _usage(self) -> ResourceUsage:
        with self._session_factory() as session:
            row = session.execute(text(_USAGE_SQL)).mappings().one()
        return ResourceUsage(
            openai_calls=int(row["calls"] or 0),
            estimated_cost_usd=float(row["cost_usd"] or 0),
        )

    def _persist(self, params: dict[str, Any]) -> bool:
        with self._session_factory() as session:
            result = session.execute(text(_INSERT), params)
            session.commit()
            return result.rowcount > 0

    async def run(self, *, limit: int = 40) -> MessageReviewSummary:
        usage = await asyncio.to_thread(self._usage)
        summary = MessageReviewSummary()
        if not evaluate_resource_budget(usage=usage, budget=self._budget)["research_enrichment_allowed"]:
            summary.skipped_budget = True
            return summary

        candidates = await asyncio.to_thread(self._select, limit)
        summary.selected = len(candidates)
        for candidate in candidates:
            context = MessageReviewContext(
                observation_id=str(candidate["observation_id"]),
                provider_name=str(candidate["provider_name"]),
                observed_at=candidate["observed_at"].isoformat(),
                current_message=str(candidate.get("current_message") or ""),
                original_decision=str(candidate["decision"]),
                original_action=str(candidate["action"]),
                original_outcome_reason=(
                    str(candidate["outcome_reason"])
                    if candidate.get("outcome_reason") is not None
                    else None
                ),
                provider_profile=self._profile_summary(candidate.get("provider_profile_snapshot")),
                recent_messages=list(candidate.get("recent_messages") or []),
            )
            try:
                review = await self._engine.review(context)
            except AidyMessageReviewUnavailable:
                summary.failed += 1
                logger.warning(
                    "AIDY message review failed observation_id=%s", context.observation_id
                )
                continue

            params = {
                "id": uuid4(),
                "observation_id": candidate["observation_id"],
                "source_id": candidate["source_id"],
                "observed_at": candidate["observed_at"],
                "original_decision": candidate["decision"],
                "original_action": candidate["action"],
                "original_outcome_reason": candidate.get("outcome_reason"),
                "review_class": review.review_class,
                "suggested_action": review.suggested_action,
                "confidence": review.confidence,
                "missing_fields": json.dumps(review.missing_fields),
                "rationale": review.rationale,
                "suggested_parser_rule": review.suggested_parser_rule,
                "provider_profile_version_no": candidate.get("provider_profile_version_no"),
                "model_version": MODEL_VERSION,
                "prompt_version": PROMPT_VERSION,
                "model_name": review.model_name,
                "response_id": review.response_id,
                "input_tokens": review.input_tokens,
                "output_tokens": review.output_tokens,
                "estimated_cost_usd": review.estimated_cost_usd,
                "latency_ms": review.latency_ms,
            }
            if await asyncio.to_thread(self._persist, params):
                summary.record(review.review_class)

            usage = ResourceUsage(
                openai_calls=usage.openai_calls + 1,
                estimated_cost_usd=usage.estimated_cost_usd + float(review.estimated_cost_usd),
            )
            if not evaluate_resource_budget(usage=usage, budget=self._budget)[
                "research_enrichment_allowed"
            ]:
                summary.skipped_budget = True
                break
        return summary


__all__ = ["AidyMessageReviewRunner", "MessageReviewSummary"]
