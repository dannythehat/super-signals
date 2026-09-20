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
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_context_client import AidyCanonicalContext, AidyContextClient, AidyContextTerminalMiss
from app.aidy_economic_calendar_client import EconomicCalendarClient
from app.aidy_event_liquidity_execution import (
    build_event_liquidity_execution_context,
    execution_calibration_from_candidate,
)
from app.aidy_evidence_contract import (
    EVIDENCE_CONTRACT_VERSION,
    build_provider_evidence_claims,
)
from app.aidy_market_client import AidyMarketClient
from app.aidy_provider_alpha_analogue import load_provider_alpha_analogue_context
from app.aidy_probability_ev_management import build_probability_ev_management_context
from app.aidy_failure_self_critique import build_failure_self_critique_context
from app.aidy_reasoning_calendar_tools import (
    build_calendar_tool_executor,
    fetch_calendar_summary,
    fetch_todays_scheduled_events,
)
from app.aidy_reasoning_engine import (
    CALENDAR_TOOL_NAME,
    CALENDAR_TOOL_SCHEMA,
    CANDLE_TOOL_NAME,
    CANDLE_TOOL_SCHEMA,
    MODEL_VERSION,
    PROMPT_VERSION,
    AidyReasoningEngine,
    AidyReasoningUnavailable,
    SignalContext,
    ToolExecutor,
)
from app.aidy_reasoning_market_tools import build_candle_tool_executor, fetch_candle_summary
from app.provider_day19_explainer_budget import (
    ResourceBudget,
    ResourceUsage,
    evaluate_resource_budget,
)

logger = logging.getLogger(__name__)

_SELECTABLE = """
    WITH execution_samples AS MATERIALIZED (
        SELECT *
        FROM provider_execution_calibration_samples
        WHERE account_environment='demo'
    )
    SELECT d.id AS decision_id, d.decision_class, d.reasons, d.source_id,
           d.signal_posted_at,
           o.message_id,o.signal_id,o.side,o.symbol,o.entry_low,o.entry_high,o.stop_loss,o.take_profits,
           m.raw_text AS current_message,
           COALESCE(NULLIF(s.chat_title, ''), s.source_alias) AS provider_name,
           COALESCE(b.trades_resolved, 0) AS trades_resolved,
           fp.summary AS provider_fingerprint_summary,
           fp.snapshot AS provider_fingerprint_snapshot,
           profile.version_no AS provider_profile_version_no,
           profile.effective_at AS provider_profile_effective_at,
           profile.profile_snapshot AS provider_profile_snapshot,
           intel.contract_version AS provider_intelligence_contract,
           intel.evidence_as_of_utc AS provider_intelligence_evidence_as_of_utc,
           intel.fingerprint_json AS provider_intelligence_fingerprint,
           intel.adaptation_json AS provider_intelligence_adaptation,
           intel.governance_json AS provider_intelligence_governance,
           ctx.signal_id AS context_signal_id,
           ctx.aidy_context_as_of_utc AS attached_context_as_of_utc,
           ctx.aidy_context_lag_seconds AS attached_context_lag_seconds,
           ctx.session_json AS attached_session_json,
           ctx.regime_json AS attached_regime_json,
           ctx.data_quality_json AS attached_data_quality_json,
           ctx.market_json AS attached_market_json,
           ctx.gold_state_json AS attached_gold_state_json,
           recent.messages_json AS recent_messages,
           calibration.calibration_json AS self_calibration,
           execution.entry_samples AS execution_entry_samples,
           execution.entry_p50 AS execution_entry_p50,
           execution.entry_p95 AS execution_entry_p95,
           execution.exit_samples AS execution_exit_samples,
           execution.exit_p50 AS execution_exit_p50,
           execution.exit_p95 AS execution_exit_p95,
           execution.contract_samples AS execution_contract_samples,
           execution.contract_p50 AS execution_contract_p50,
           execution.charge_samples AS execution_charge_samples,
           execution.charge_p50 AS execution_charge_p50,
           execution.charge_p95 AS execution_charge_p95,
           execution.evidence_as_of_utc AS execution_evidence_as_of_utc
    FROM aidy_decisions d
    JOIN provider_trade_observations o ON o.id=d.observation_id
    JOIN sources s ON s.id=d.source_id
    JOIN messages m ON m.id=o.message_id
    LEFT JOIN provider_trade_scoreboard b ON b.source_id=d.source_id
    LEFT JOIN aidy_reasoning_annotations a ON a.decision_id=d.id
    LEFT JOIN LATERAL (
        SELECT f.summary,to_jsonb(f) AS snapshot
        FROM provider_trade_fingerprints f
        WHERE f.source_id=d.source_id
          AND f.computed_at<=d.signal_posted_at
        ORDER BY f.computed_at DESC
        LIMIT 1
    ) fp ON true
    LEFT JOIN LATERAL (
        SELECT v.version_no,v.effective_at,v.profile_snapshot
        FROM provider_research_profile_versions v
        WHERE v.source_id=d.source_id
          AND v.effective_at<=d.signal_posted_at
        ORDER BY v.effective_at DESC,v.version_no DESC
        LIMIT 1
    ) profile ON true
    LEFT JOIN LATERAL (
        SELECT i.contract_version,i.evidence_as_of_utc,i.fingerprint_json,i.adaptation_json,i.governance_json
        FROM provider_intelligence_snapshots i
        WHERE i.source_id=d.source_id
          AND i.evidence_as_of_utc<=d.signal_posted_at
        ORDER BY i.evidence_as_of_utc DESC,i.created_at DESC
        LIMIT 1
    ) intel ON true
    LEFT JOIN LATERAL (
        SELECT x.signal_id,x.aidy_context_as_of_utc,x.aidy_context_lag_seconds,
               x.session_json,x.regime_json,x.data_quality_json,x.market_json,x.gold_state_json
        FROM provider_signal_context_attachments x
        WHERE (o.signal_id IS NOT NULL AND x.signal_id=o.signal_id)
           OR x.message_id=o.message_id
        ORDER BY (o.signal_id IS NOT NULL AND x.signal_id=o.signal_id) DESC,x.created_at DESC
        LIMIT 1
    ) ctx ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(
                   jsonb_build_object('posted_at',q.posted_at,'text',q.raw_text)
                   ORDER BY q.posted_at
               ) AS messages_json
        FROM (
            SELECT mm.posted_at,left(mm.raw_text,700) AS raw_text
            FROM messages mm
            WHERE mm.source_id=d.source_id
              AND mm.posted_at<=d.signal_posted_at
            ORDER BY mm.posted_at DESC
            LIMIT 5
        ) q
    ) recent ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_object_agg(z.lean,z.stats) AS calibration_json
        FROM (
            SELECT ar.lean,
                   jsonb_build_object(
                       'resolved',count(*),
                       'wins',count(*) FILTER (WHERE ao.actual_pnl_usd>0),
                       'losses',count(*) FILTER (WHERE ao.actual_pnl_usd<0),
                       'avg_pnl_usd',round(avg(ao.actual_pnl_usd),2),
                       'total_pnl_usd',round(sum(ao.actual_pnl_usd),2)
                   ) AS stats
            FROM aidy_reasoning_annotations ar
            JOIN aidy_decisions prior_d ON prior_d.id=ar.decision_id
            JOIN aidy_decision_outcomes ao ON ao.decision_id=prior_d.id
            WHERE prior_d.source_id=d.source_id
              AND ar.created_at<d.signal_posted_at
              AND ao.resolved_at<d.signal_posted_at
              AND ao.actual_pnl_usd IS NOT NULL
            GROUP BY ar.lean
        ) z
    ) calibration ON true
    LEFT JOIN LATERAL (
        SELECT
            COUNT(c.entry_adverse_slippage_points)::int AS entry_samples,
            GREATEST(
                percentile_cont(0.50) WITHIN GROUP (
                    ORDER BY c.entry_adverse_slippage_points
                )::numeric,
                0
            ) AS entry_p50,
            GREATEST(
                percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY c.entry_adverse_slippage_points
                )::numeric,
                0
            ) AS entry_p95,
            COUNT(c.exit_adverse_slippage_points)::int AS exit_samples,
            GREATEST(
                percentile_cont(0.50) WITHIN GROUP (
                    ORDER BY c.exit_adverse_slippage_points
                )::numeric,
                0
            ) AS exit_p50,
            GREATEST(
                percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY c.exit_adverse_slippage_points
                )::numeric,
                0
            ) AS exit_p95,
            COUNT(c.implied_usd_per_point_per_lot) FILTER (
                WHERE c.implied_usd_per_point_per_lot>0
                  AND c.implied_usd_per_point_per_lot<1000
            )::int AS contract_samples,
            (
                percentile_cont(0.50) WITHIN GROUP (
                    ORDER BY c.implied_usd_per_point_per_lot
                ) FILTER (
                    WHERE c.implied_usd_per_point_per_lot>0
                      AND c.implied_usd_per_point_per_lot<1000
                )
            )::numeric AS contract_p50,
            COUNT(c.broker_cash_charge_usd_per_lot) FILTER (
                WHERE c.broker_cash_charge_usd_per_lot IS NOT NULL
            )::int AS charge_samples,
            (
                percentile_cont(0.50) WITHIN GROUP (
                    ORDER BY GREATEST(c.broker_cash_charge_usd_per_lot,0)
                ) FILTER (
                    WHERE c.broker_cash_charge_usd_per_lot IS NOT NULL
                )
            )::numeric AS charge_p50,
            (
                percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY GREATEST(c.broker_cash_charge_usd_per_lot,0)
                ) FILTER (
                    WHERE c.broker_cash_charge_usd_per_lot IS NOT NULL
                )
            )::numeric AS charge_p95,
            MAX(c.closed_at) AS evidence_as_of_utc
        FROM execution_samples c
        WHERE c.closed_at<=d.signal_posted_at
    ) execution ON true
    WHERE d.decision_class='approve'
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
        id, decision_id, lean, confidence,
        gold_view_direction, gold_view_confidence, gold_view_horizon_minutes,
        gold_view_reason, provider_alignment,
        rationale, key_factors,
        model_version, prompt_version, model_name, response_id,
        input_tokens, output_tokens, estimated_cost_usd, latency_ms,
        market_context_available, provider_context_available,
        provider_profile_version_no, request_count, tool_calls_made,
        preflight_evidence_calls, shadow_action, shadow_risk_multiplier,
        shadow_action_reason, evidence_contract_version, provider_evidence_snapshot,
        provider_claim_refs, claim_validation_status, unsupported_claim_count
    ) VALUES (
        :id, :decision_id, :lean, :confidence,
        :gold_view_direction, :gold_view_confidence, :gold_view_horizon_minutes,
        :gold_view_reason, :provider_alignment,
        :rationale, CAST(:key_factors AS jsonb),
        :model_version, :prompt_version, :model_name, :response_id,
        :input_tokens, :output_tokens, :estimated_cost_usd, :latency_ms,
        :market_context_available, :provider_context_available,
        :provider_profile_version_no, :request_count, :tool_calls_made,
        :preflight_evidence_calls, :shadow_action, :shadow_risk_multiplier,
        :shadow_action_reason, :evidence_contract_version,
        CAST(:provider_evidence_snapshot AS jsonb), CAST(:provider_claim_refs AS jsonb),
        :claim_validation_status, :unsupported_claim_count
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
        calendar_client: EconomicCalendarClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine
        self._budget = budget or _budget_from_environment()
        self._context_client = context_client
        self._candle_client = candle_client
        self._calendar_client = calendar_client

    def _tools_for(self, signal_posted_at) -> tuple[list[dict], ToolExecutor | None]:
        """Combine whichever tool clients are actually configured into one schema list and
        one dispatching executor. A tool whose client isn't configured is simply absent from
        both -- never offered, so the model can't call something no executor can honour."""
        schemas: list[dict] = []
        executors: dict[str, ToolExecutor] = {}

        candle_executor = build_candle_tool_executor(
            self._candle_client, signal_posted_at=signal_posted_at
        )
        if candle_executor is not None:
            schemas.append(CANDLE_TOOL_SCHEMA)
            executors[CANDLE_TOOL_NAME] = candle_executor

        calendar_executor = build_calendar_tool_executor(
            self._calendar_client, signal_posted_at=signal_posted_at
        )
        if calendar_executor is not None:
            schemas.append(CALENDAR_TOOL_SCHEMA)
            executors[CALENDAR_TOOL_NAME] = calendar_executor

        if not executors:
            return [], None

        async def dispatch(name: str, arguments: dict) -> dict:
            executor = executors.get(name)
            if executor is None:
                return {"error": "unknown_tool"}
            return await executor(name, arguments)

        return schemas, dispatch

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
            "market": context.market or {},
            "gold_state": context.gold_state or {},
        }

    @staticmethod
    def _provider_brain(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        profile = candidate.get("provider_profile_snapshot")
        profile_dict = dict(profile) if isinstance(profile, dict) else {}
        metadata = profile_dict.get("profile_metadata") if isinstance(profile_dict.get("profile_metadata"), dict) else {}
        adaptive = metadata.get("adaptive_v1") if isinstance(metadata.get("adaptive_v1"), dict) else {}
        footprint = metadata.get("footprint_v1") if isinstance(metadata.get("footprint_v1"), dict) else {}
        language = adaptive.get("language") if isinstance(adaptive.get("language"), dict) else {}
        interpretation = footprint.get("interpretation_context") if isinstance(footprint.get("interpretation_context"), dict) else {}
        performance = adaptive.get("performance") if isinstance(adaptive.get("performance"), dict) else {}

        profile_summary = None
        if profile_dict:
            profile_summary = {
                "version_no": candidate.get("provider_profile_version_no"),
                "effective_at": (
                    candidate["provider_profile_effective_at"].isoformat()
                    if candidate.get("provider_profile_effective_at") is not None
                    else None
                ),
                "research_state": profile_dict.get("research_state"),
                "style": profile_dict.get("style"),
                "interpretation_readiness": profile_dict.get("interpretation_readiness"),
                "observed_messages": profile_dict.get("observed_messages"),
                "structured_signal_messages": profile_dict.get("structured_signal_messages"),
                "management_messages": profile_dict.get("management_messages"),
                "interpretation_context": interpretation,
                "language": {
                    "cadence_bucket": language.get("cadence_bucket"),
                    "sequence_bucket": language.get("sequence_bucket"),
                    "entry_bucket": language.get("entry_bucket"),
                    "order_bucket": language.get("order_bucket"),
                    "management_bucket": language.get("management_bucket"),
                    "traits": language.get("traits") or {},
                    "grammar_examples_masked": language.get("grammar_examples_masked") or {},
                },
                "performance": performance,
            }

        intelligence = None
        if any(
            candidate.get(key) is not None
            for key in (
                "provider_intelligence_fingerprint",
                "provider_intelligence_adaptation",
                "provider_intelligence_governance",
            )
        ):
            intelligence = {
                "contract_version": candidate.get("provider_intelligence_contract"),
                "evidence_as_of_utc": (
                    candidate["provider_intelligence_evidence_as_of_utc"].isoformat()
                    if candidate.get("provider_intelligence_evidence_as_of_utc") is not None
                    else None
                ),
                "fingerprint": candidate.get("provider_intelligence_fingerprint") or {},
                "adaptation": candidate.get("provider_intelligence_adaptation") or {},
                "governance": candidate.get("provider_intelligence_governance") or {},
            }
        return profile_summary, intelligence

    @staticmethod
    def _attached_market_context(candidate: dict[str, Any]) -> dict[str, Any] | None:
        if candidate.get("context_signal_id") is None:
            return None
        regime = candidate.get("attached_regime_json") or {}
        labels = regime.get("labels") if isinstance(regime, dict) else {}
        labels = labels if isinstance(labels, dict) else {}
        trend_evidence = (
            ((regime.get("rule_evidence") or {}).get("trend_structure") or {})
            if isinstance(regime, dict)
            else {}
        )
        data_quality = candidate.get("attached_data_quality_json") or {}
        data_quality = data_quality if isinstance(data_quality, dict) else {}
        return {
            "source": "immutable_signal_attachment",
            "as_of_utc": (
                candidate["attached_context_as_of_utc"].isoformat()
                if candidate.get("attached_context_as_of_utc") is not None
                else None
            ),
            "context_lag_seconds": candidate.get("attached_context_lag_seconds"),
            "session": labels.get("session"),
            "trend_structure": labels.get("trend_structure"),
            "trend_by_timeframe": trend_evidence.get("directions"),
            "volatility_band": labels.get("volatility_band"),
            "event_timing": labels.get("event_timing"),
            "quote_freshness": data_quality.get("quote_freshness"),
            "quote_state": data_quality.get("quote_state"),
            "market": candidate.get("attached_market_json") or {},
            "gold_state": candidate.get("attached_gold_state_json") or {},
        }

    def _live_toolbox_manifest(
        self,
        *,
        market_context: dict[str, Any] | None,
        provider_evidence_claims: list[dict[str, Any]],
        recent_messages: list[Any],
        event_liquidity_execution_context: dict[str, Any] | None,
        provider_alpha_analogue_context: dict[str, Any] | None,
        probability_ev_management_context: dict[str, Any] | None,
        failure_self_critique_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Tell AIDY exactly which capabilities exist for this decision.

        This is capability awareness, not an instruction to call everything. It prevents a
        silent failure mode where a built surface is ignored or an unavailable one is guessed.
        """
        movement = (
            market_context.get("gold_state", {}).get("movement_investigation", {})
            if isinstance(market_context, dict)
            and isinstance(market_context.get("gold_state"), dict)
            else {}
        )
        standing = {
            "provider_history": bool(provider_evidence_claims),
            "market_context": bool(market_context),
            "gold_movement_investigation": bool(movement),
            "recent_messages": bool(recent_messages),
            "event_liquidity": bool(event_liquidity_execution_context),
            "historical_analogues": bool(provider_alpha_analogue_context),
            "probability_ev": bool(probability_ev_management_context),
            "self_critique": bool(failure_self_critique_context),
        }
        gold_state = (
            market_context.get("gold_state")
            if isinstance(market_context, dict)
            and isinstance(market_context.get("gold_state"), dict)
            else {}
        )
        research = (
            gold_state.get("research_surfaces")
            if isinstance(gold_state, dict)
            and isinstance(gold_state.get("research_surfaces"), dict)
            else {}
        )
        return {
            "contract_version": "aidy_live_toolbox_manifest_v1",
            "decision_rule": (
                "Consider every available standing surface. Call a tool when it can materially "
                "resolve uncertainty relevant to take/reduce/reject. Never invent unavailable evidence."
            ),
            "on_demand_tools": [
                {
                    "name": CANDLE_TOOL_NAME,
                    "status": "available" if self._candle_client is not None else "unavailable",
                    "use_when": "recent price structure could materially change the decision",
                },
                {
                    "name": CALENDAR_TOOL_NAME,
                    "status": "available" if self._calendar_client is not None else "unavailable",
                    "use_when": "scheduled macro-event proximity could materially change holding risk",
                },
            ],
            "standing_surfaces": [
                {
                    "surface": name,
                    "status": "available" if available else "unknown_unavailable",
                }
                for name, available in standing.items()
            ],
            "research_surfaces": [
                {
                    "surface": name,
                    "state": (
                        str(value.get("state") or "unknown")
                        if isinstance(value, dict)
                        else "unknown"
                    ),
                    "callable": False,
                    "mode": "standing_context_state_only",
                }
                for name, value in sorted(research.items())
            ],
            "target_outcome_available": False,
            "live_execution_authority": False,
        }

    async def _prefetch_evidence(
        self, *, signal_posted_at, market_context: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, int]:
        evidence: dict[str, Any] = {}
        calls = 0
        gold_state = (
            (market_context or {}).get("gold_state")
            if isinstance((market_context or {}).get("gold_state"), dict)
            else {}
        )
        movement = (
            gold_state.get("movement_investigation")
            if isinstance(gold_state.get("movement_investigation"), dict)
            else {}
        )
        investigation_required = movement.get("investigation_required") is True
        requested_follow_up = set(movement.get("required_follow_up_tools") or [])

        if self._candle_client is not None:
            evidence["m15_structure"] = await fetch_candle_summary(
                self._candle_client,
                as_of=signal_posted_at,
                timeframe_minutes=15,
                lookback_count=8,
            )
            calls += 1
            if investigation_required:
                evidence["spike_m5_structure"] = await fetch_candle_summary(
                    self._candle_client,
                    as_of=signal_posted_at,
                    timeframe_minutes=5,
                    lookback_count=12,
                )
                calls += 1
            trend = str((market_context or {}).get("trend_structure") or "unknown").lower()
            if trend in {"mixed", "range", "unknown", "none"}:
                evidence["h1_structure"] = await fetch_candle_summary(
                    self._candle_client,
                    as_of=signal_posted_at,
                    timeframe_minutes=60,
                    lookback_count=6,
                )
                calls += 1

        event_timing = str((market_context or {}).get("event_timing") or "unknown").lower()
        calendar_requested = "economic_calendar" in requested_follow_up
        if self._calendar_client is not None and (
            event_timing in {"unknown", "blocked", "none", ""} or calendar_requested
        ):
            evidence["nearby_high_impact_events"] = await fetch_calendar_summary(
                self._calendar_client,
                as_of=signal_posted_at,
                hours_before=6,
                hours_after=6,
                min_impact="high",
            )
            calls += 1

        if investigation_required:
            evidence["gold_movement_follow_up_status"] = {
                "required_follow_up_tools": sorted(requested_follow_up),
                "connected_now": sorted(
                    name
                    for name in requested_follow_up
                    if name == "economic_calendar" and self._calendar_client is not None
                ),
                "still_unavailable": sorted(
                    name
                    for name in requested_follow_up
                    if name != "economic_calendar"
                ),
            }

        return (evidence or None), calls

    async def _fetch_market_context(self, signal_posted_at) -> dict | None:
        """Best-effort only, on two independent sources that either may or may not be
        configured: regime/session context from AidyContextClient, and -- per the owner's
        explicit direction to have AIDY 'map the trading day out... refer to it throughout
        the day' -- today's scheduled high/medium-impact calendar events, folded in as a
        standing field rather than left to the model to decide whether to ask for. A signal
        reasoned long after it posted has no live context left to fetch for either
        (AidyContextTerminalMiss(pit_context_stale) for the first, an old-signal refusal for
        the second), and any other lookup failure must never block reasoning about the
        signal's own geometry -- each source degrades independently, never both at once just
        because one failed.
        """
        summary: dict[str, Any] = {}

        if self._context_client is not None:
            try:
                context = await self._context_client.fetch_context(as_of=signal_posted_at)
                summary.update(self._market_context_summary(context))
            except AidyContextTerminalMiss:
                pass
            except Exception:  # noqa: BLE001 - a context lookup must never fail the pass
                logger.warning(
                    "AIDY market context lookup failed as_of=%s", signal_posted_at, exc_info=True
                )

        if self._calendar_client is not None:
            try:
                events = await fetch_todays_scheduled_events(
                    self._calendar_client, as_of=signal_posted_at
                )
                if events is not None:
                    summary["todays_scheduled_events"] = events
            except Exception:  # noqa: BLE001 - a calendar lookup must never fail the pass
                logger.warning(
                    "AIDY day-map lookup failed as_of=%s", signal_posted_at, exc_info=True
                )

        return summary or None

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
            market_context = self._attached_market_context(candidate)
            if market_context is None:
                market_context = await self._fetch_market_context(candidate["signal_posted_at"])
            elif self._calendar_client is not None:
                try:
                    day_events = await fetch_todays_scheduled_events(
                        self._calendar_client, as_of=candidate["signal_posted_at"]
                    )
                    if day_events is not None:
                        market_context["todays_scheduled_events"] = day_events
                except Exception:
                    logger.warning(
                        "AIDY attached-context day map failed as_of=%s",
                        candidate["signal_posted_at"],
                        exc_info=True,
                    )

            provider_profile, provider_intelligence = self._provider_brain(candidate)
            provider_evidence_claims = build_provider_evidence_claims(
                provider_profile=provider_profile,
                provider_intelligence=provider_intelligence,
                provider_fingerprint=(
                    dict(candidate["provider_fingerprint_snapshot"])
                    if isinstance(candidate.get("provider_fingerprint_snapshot"), dict)
                    else None
                ),
                signal_side=str(candidate.get("side") or ""),
                signal_session=str((market_context or {}).get("session") or ""),
            )
            supplemental_evidence, preflight_calls = await self._prefetch_evidence(
                signal_posted_at=candidate["signal_posted_at"],
                market_context=market_context,
            )
            event_liquidity_execution_context = build_event_liquidity_execution_context(
                signal_posted_at=candidate["signal_posted_at"],
                side=str(candidate.get("side") or ""),
                entry_low=candidate.get("entry_low"),
                entry_high=candidate.get("entry_high"),
                stop_loss=candidate.get("stop_loss"),
                take_profits=list(candidate.get("take_profits") or []),
                market_context=market_context,
                execution_calibration=execution_calibration_from_candidate(candidate),
            )
            build3_target_payload = {
                "signal": {
                    "side": str(candidate.get("side") or ""),
                    "symbol": str(candidate.get("symbol") or ""),
                    "entry_low": (
                        str(candidate["entry_low"])
                        if candidate.get("entry_low") is not None
                        else None
                    ),
                    "entry_high": (
                        str(candidate["entry_high"])
                        if candidate.get("entry_high") is not None
                        else None
                    ),
                    "stop_loss": (
                        str(candidate["stop_loss"])
                        if candidate.get("stop_loss") is not None
                        else None
                    ),
                    "take_profits": list(candidate.get("take_profits") or []),
                },
                "market_context": market_context,
                "event_liquidity_execution_context": event_liquidity_execution_context,
            }
            provider_alpha_analogue_context = await asyncio.to_thread(
                load_provider_alpha_analogue_context,
                self._session_factory,
                signal_posted_at=candidate["signal_posted_at"],
                source_id=candidate["source_id"],
                side=str(candidate.get("side") or ""),
                market_context=market_context,
                target_payload=build3_target_payload,
            )
            probability_ev_management_context = build_probability_ev_management_context(
                signal=build3_target_payload["signal"],
                build2_context=event_liquidity_execution_context,
                build3_context=provider_alpha_analogue_context,
            )
            self_calibration = (
                dict(candidate["self_calibration"])
                if isinstance(candidate.get("self_calibration"), dict)
                else None
            )
            failure_self_critique_context = build_failure_self_critique_context(
                market_context=market_context,
                build2_context=event_liquidity_execution_context,
                build3_context=provider_alpha_analogue_context,
                build4_context=probability_ev_management_context,
                self_calibration=self_calibration,
            )
            if supplemental_evidence is None:
                supplemental_evidence = {}
            supplemental_evidence["toolbox_manifest"] = self._live_toolbox_manifest(
                market_context=market_context,
                provider_evidence_claims=provider_evidence_claims,
                recent_messages=list(candidate.get("recent_messages") or []),
                event_liquidity_execution_context=event_liquidity_execution_context,
                provider_alpha_analogue_context=provider_alpha_analogue_context,
                probability_ev_management_context=probability_ev_management_context,
                failure_self_critique_context=failure_self_critique_context,
            )
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
                provider_evidence_claims=provider_evidence_claims,
                market_context=market_context,
                provider_intelligence=provider_intelligence,
                provider_profile=provider_profile,
                recent_messages=list(candidate.get("recent_messages") or []),
                self_calibration=self_calibration,
                supplemental_evidence=supplemental_evidence,
                event_liquidity_execution_context=event_liquidity_execution_context,
                provider_alpha_analogue_context=provider_alpha_analogue_context,
                probability_ev_management_context=probability_ev_management_context,
                failure_self_critique_context=failure_self_critique_context,
                preflight_evidence_calls=preflight_calls,
            )
            tool_schemas, tool_executor = self._tools_for(candidate["signal_posted_at"])
            try:
                annotation = await self._engine.reason(
                    context, tool_executor=tool_executor, tool_schemas=tool_schemas
                )
            except AidyReasoningUnavailable:
                summary.failed += 1
                logger.warning("AIDY reasoning call failed decision_id=%s", context.decision_id)
                continue

            row = {
                "id": uuid4(),
                "decision_id": candidate["decision_id"],
                "lean": annotation.lean,
                "confidence": annotation.confidence,
                "gold_view_direction": annotation.gold_view_direction,
                "gold_view_confidence": annotation.gold_view_confidence,
                "gold_view_horizon_minutes": annotation.gold_view_horizon_minutes,
                "gold_view_reason": annotation.gold_view_reason,
                "provider_alignment": annotation.provider_alignment,
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
                "provider_context_available": (
                    provider_profile is not None or provider_intelligence is not None
                ),
                "provider_profile_version_no": candidate.get("provider_profile_version_no"),
                "request_count": annotation.request_count,
                "tool_calls_made": annotation.tool_calls_made,
                "preflight_evidence_calls": annotation.preflight_evidence_calls,
                "shadow_action": annotation.shadow_action,
                "shadow_risk_multiplier": annotation.risk_multiplier,
                "shadow_action_reason": annotation.action_reason,
                "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
                "provider_evidence_snapshot": json.dumps(provider_evidence_claims, default=str),
                "provider_claim_refs": json.dumps(list(annotation.provider_claim_refs)),
                "claim_validation_status": "passed",
                "unsupported_claim_count": 0,
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
            calendar_client=EconomicCalendarClient.from_environment(),
        )
        summary = await runner.run(limit=args.limit)
    finally:
        engine.dispose()
    print(summary.as_text(), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(asyncio.run(_main()))


__all__ = ["AidyReasoningRunner", "ReasoningSummary"]
