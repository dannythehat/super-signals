"""Strict as-of historical time machine for AIDY research.

The replay path is intentionally separate from live reasoning and execution. Historical
inputs are materialized without selecting outcome values, hashed, and frozen. Only after a
model decision has been persisted does a separate scoring phase join the future outcome.

The default runtime never opens the holdout partition.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_evidence_contract import build_provider_evidence_claims
from app.aidy_event_liquidity_execution import (
    build_event_liquidity_execution_context,
    execution_calibration_from_candidate,
)
from app.aidy_provider_alpha_analogue import load_provider_alpha_analogue_context
from app.aidy_probability_ev_management import build_probability_ev_management_context
from app.aidy_failure_self_critique import (
    build_failure_self_critique_context,
    load_replay_self_feedback,
)
from app.aidy_reasoning_engine import (
    MODEL_VERSION,
    PROMPT_VERSION,
    AidyReasoningEngine,
    AidyReasoningUnavailable,
    SignalContext,
)
from app.aidy_reasoning_runner import AidyReasoningRunner

logger = logging.getLogger(__name__)

REPLAY_VERSION = "aidy_historical_time_machine_v8"
INPUT_CONTRACT_VERSION = "aidy_historical_replay_input_v7"

# Frozen partition cutoffs from the first exact-PIT eligible cohort on 2026-09-19.
# These cutoffs never move when later rows are added.
_DEVELOPMENT_END = datetime(2026, 9, 17, 13, 20, 25, tzinfo=UTC)
_VALIDATION_END = datetime(2026, 9, 18, 8, 32, 44, tzinfo=UTC)

_DEFAULT_INTERVAL_SECONDS = 30
_DEFAULT_BATCH = 12
_DEFAULT_MAX_CALLS = 220
_PROVIDER_CLAIM_RETRY_LIMIT = 1
_EXPECTED_FROZEN_CASES = 140
_PREVIOUS_INPUT_CONTRACT_VERSION = "aidy_historical_replay_input_v6"

_LOAD_PREVIOUS_CASES = text(
    """
    SELECT source_decision_id,source_id,signal_posted_at,partition,input_payload
    FROM aidy_historical_replay_cases
    WHERE input_contract_version=:previous_input_contract_version
    ORDER BY signal_posted_at,id
    LIMIT :limit
    """
)

_FORBIDDEN_INPUT_KEYS = {
    "actual_pnl_usd",
    "actual_realized_r",
    "baseline_pnl_usd",
    "baseline_realized_r",
    "decision_delta_usd",
    "outcome",
    "outcome_resolved_at",
    "realized_pnl",
    "resolution",
    "resolved_at",
    "shadow_pnl_usd",
    "target_hit",
    "stop_hit",
    "future_return",
    "future_returns",
    "mfe",
    "mae",
}

_MATERIALIZE_SELECT = text(
    """
    WITH execution_samples AS MATERIALIZED (
        SELECT *
        FROM provider_execution_calibration_samples
        WHERE account_environment='demo'
    )
    SELECT d.id AS source_decision_id,d.source_id,
           d.signal_posted_at,
           o.message_id,o.signal_id,o.side,o.symbol,o.entry_low,o.entry_high,o.stop_loss,o.take_profits,
           COALESCE(NULLIF(s.chat_title,''),s.source_alias,'UNKNOWN') AS provider_name,
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
    LEFT JOIN aidy_historical_replay_cases rc
      ON rc.source_decision_id=d.id
     AND rc.input_contract_version=:input_contract_version
    LEFT JOIN LATERAL (
        SELECT to_jsonb(f) AS snapshot
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
        SELECT i.contract_version,i.evidence_as_of_utc,
               i.fingerprint_json,i.adaptation_json,i.governance_json
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
        WHERE ((o.signal_id IS NOT NULL AND x.signal_id=o.signal_id) OR x.message_id=o.message_id)
          AND x.aidy_context_as_of_utc<=d.signal_posted_at
        ORDER BY (o.signal_id IS NOT NULL AND x.signal_id=o.signal_id) DESC,
                 x.aidy_context_as_of_utc DESC,x.created_at DESC
        LIMIT 1
    ) ctx ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(
                   jsonb_build_object(
                       'posted_at',q.posted_at,
                       'text',q.effective_text,
                       'revision_index_as_of_signal',q.effective_revision_index
                   )
                   ORDER BY q.posted_at
               ) AS messages_json
        FROM (
            SELECT mm.posted_at,
                   left(COALESCE(mr.raw_text,mm.raw_text),700) AS effective_text,
                   COALESCE(mr.revision_index,0) AS effective_revision_index
            FROM messages mm
            LEFT JOIN LATERAL (
                SELECT rev.raw_text,rev.revision_index
                FROM message_revisions rev
                WHERE rev.message_id=mm.id
                  AND rev.edited_at<=d.signal_posted_at
                ORDER BY rev.revision_index DESC,rev.edited_at DESC
                LIMIT 1
            ) mr ON true
            WHERE mm.source_id=d.source_id
              AND mm.posted_at<=d.signal_posted_at
              AND (mm.deleted_at IS NULL OR mm.deleted_at>d.signal_posted_at)
            ORDER BY mm.posted_at DESC,mm.telegram_message_id DESC
            LIMIT 5
        ) q
    ) recent ON true
    LEFT JOIN LATERAL (
        SELECT
            COUNT(c.entry_adverse_slippage_points)::int AS entry_samples,
            GREATEST(percentile_cont(0.50) WITHIN GROUP (
                ORDER BY c.entry_adverse_slippage_points
            )::numeric,0) AS entry_p50,
            GREATEST(percentile_cont(0.95) WITHIN GROUP (
                ORDER BY c.entry_adverse_slippage_points
            )::numeric,0) AS entry_p95,
            COUNT(c.exit_adverse_slippage_points)::int AS exit_samples,
            GREATEST(percentile_cont(0.50) WITHIN GROUP (
                ORDER BY c.exit_adverse_slippage_points
            )::numeric,0) AS exit_p50,
            GREATEST(percentile_cont(0.95) WITHIN GROUP (
                ORDER BY c.exit_adverse_slippage_points
            )::numeric,0) AS exit_p95,
            COUNT(c.implied_usd_per_point_per_lot) FILTER (
                WHERE c.implied_usd_per_point_per_lot>0
                  AND c.implied_usd_per_point_per_lot<1000
            )::int AS contract_samples,
            (percentile_cont(0.50) WITHIN GROUP (
                ORDER BY c.implied_usd_per_point_per_lot
            ) FILTER (
                WHERE c.implied_usd_per_point_per_lot>0
                  AND c.implied_usd_per_point_per_lot<1000
            ))::numeric AS contract_p50,
            COUNT(c.broker_cash_charge_usd_per_lot) FILTER (
                WHERE c.broker_cash_charge_usd_per_lot IS NOT NULL
            )::int AS charge_samples,
            (percentile_cont(0.50) WITHIN GROUP (
                ORDER BY GREATEST(c.broker_cash_charge_usd_per_lot,0)
            ) FILTER (
                WHERE c.broker_cash_charge_usd_per_lot IS NOT NULL
            ))::numeric AS charge_p50,
            (percentile_cont(0.95) WITHIN GROUP (
                ORDER BY GREATEST(c.broker_cash_charge_usd_per_lot,0)
            ) FILTER (
                WHERE c.broker_cash_charge_usd_per_lot IS NOT NULL
            ))::numeric AS charge_p95,
            MAX(c.closed_at) AS evidence_as_of_utc
        FROM execution_samples c
        WHERE c.closed_at<=d.signal_posted_at
    ) execution ON true
    WHERE d.decision_class='approve'
      AND rc.id IS NULL
      AND profile.version_no IS NOT NULL
      AND ctx.aidy_context_as_of_utc IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM aidy_decision_outcomes ao WHERE ao.decision_id=d.id
      )
    ORDER BY d.signal_posted_at,d.id
    LIMIT :limit
    """
)

_INSERT_CASE = text(
    """
    INSERT INTO aidy_historical_replay_cases (
        id,source_decision_id,source_id,signal_posted_at,partition,evidence_tier,
        input_contract_version,input_payload,input_digest,model_eligible,
        research_only,live_money_execution_allowed
    ) VALUES (
        :id,:source_decision_id,:source_id,:signal_posted_at,:partition,'exact_pit',
        :input_contract_version,CAST(:input_payload AS jsonb),:input_digest,:model_eligible,
        true,false
    )
    ON CONFLICT (source_decision_id,input_contract_version) DO NOTHING
    """
)

_SELECT_CASES = text(
    """
    SELECT c.*
    FROM aidy_historical_replay_cases c
    LEFT JOIN aidy_historical_replay_decisions rd
      ON rd.case_id=c.id AND rd.replay_version=:replay_version
    WHERE rd.id IS NULL
      AND c.input_contract_version=:input_contract_version
      AND c.model_eligible=true
      AND (
        (:scope='development' AND c.partition='development')
        OR (:scope='development_validation' AND c.partition IN ('development','validation'))
        OR (:scope='holdout' AND c.partition='holdout')
      )
    ORDER BY c.signal_posted_at,c.id
    LIMIT :limit
    """
)

_INSERT_DECISION = text(
    """
    INSERT INTO aidy_historical_replay_decisions (
        id,case_id,replay_version,model_version,prompt_version,model_name,input_digest,
        output_payload,response_id,input_tokens,output_tokens,estimated_cost_usd,latency_ms,
        research_only,live_money_execution_allowed
    ) VALUES (
        :id,:case_id,:replay_version,:model_version,:prompt_version,:model_name,:input_digest,
        CAST(:output_payload AS jsonb),:response_id,:input_tokens,:output_tokens,
        :estimated_cost_usd,:latency_ms,true,false
    )
    ON CONFLICT (case_id,replay_version) DO NOTHING
    """
)

_SELECT_UNSCORED = text(
    """
    SELECT rd.id AS replay_decision_id,rd.output_payload,c.source_decision_id,c.partition,
           ao.baseline_pnl_usd,ao.baseline_realized_r,ao.actual_pnl_usd,ao.actual_realized_r,
           ao.resolved_at,ao.resolution
    FROM aidy_historical_replay_decisions rd
    JOIN aidy_historical_replay_cases c ON c.id=rd.case_id
    JOIN aidy_decision_outcomes ao ON ao.decision_id=c.source_decision_id
    LEFT JOIN aidy_historical_replay_scores rs ON rs.replay_decision_id=rd.id
    WHERE rs.id IS NULL
      AND rd.replay_version=:replay_version
    ORDER BY rd.decided_at,rd.id
    LIMIT :limit
    """
)

_INSERT_SCORE = text(
    """
    INSERT INTO aidy_historical_replay_scores (
        id,replay_decision_id,source_decision_id,partition,resolution,
        baseline_pnl_usd,baseline_realized_r,actual_pnl_usd,actual_realized_r,
        replay_shadow_pnl_usd,replay_delta_vs_taken_usd,outcome_resolved_at,
        research_only,live_money_execution_allowed
    ) VALUES (
        :id,:replay_decision_id,:source_decision_id,:partition,:resolution,
        :baseline_pnl_usd,:baseline_realized_r,:actual_pnl_usd,:actual_realized_r,
        :replay_shadow_pnl_usd,:replay_delta_vs_taken_usd,:outcome_resolved_at,
        true,false
    )
    ON CONFLICT (replay_decision_id) DO NOTHING
    """
)

_COUNT_DECISIONS = text(
    "SELECT count(*) FROM aidy_historical_replay_decisions WHERE replay_version=:replay_version"
)

_INSERT_RUN = text(
    """
    INSERT INTO aidy_historical_replay_runs (
        id,replay_version,model_version,prompt_version,partition_scope,
        cases_materialized,decisions_written,decisions_failed,scores_written,
        total_replay_delta_usd,total_replay_shadow_pnl_usd,holdout_opened,
        research_only,live_money_execution_allowed,started_at,finished_at,details
    ) VALUES (
        :id,:replay_version,:model_version,:prompt_version,:partition_scope,
        :cases_materialized,:decisions_written,:decisions_failed,:scores_written,
        :total_replay_delta_usd,:total_replay_shadow_pnl_usd,:holdout_opened,
        true,false,:started_at,:finished_at,CAST(:details AS jsonb)
    )
    """
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode()).hexdigest()


def _partition(signal_at: datetime) -> str:
    at = signal_at.astimezone(UTC)
    if at <= _DEVELOPMENT_END:
        return "development"
    if at <= _VALIDATION_END:
        return "validation"
    return "holdout"


def _assert_no_future_fields(value: Any, *, path: str = "input") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_INPUT_KEYS:
                raise ValueError(f"historical_replay_future_field:{path}.{key}")
            _assert_no_future_fields(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_future_fields(item, path=f"{path}[{index}]")


def _dt_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _scope_from_env() -> str:
    scope = os.getenv("AIDY_HISTORICAL_REPLAY_SCOPE", "development_validation").strip()
    if scope not in {"development", "development_validation", "holdout"}:
        return "development_validation"
    if scope == "holdout" and os.getenv("AIDY_HISTORICAL_REPLAY_OPEN_HOLDOUT", "0").strip() != "1":
        return "development_validation"
    return scope


def _api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("Open", "").strip()


def _signal_context_from_payload(payload: dict[str, Any]) -> SignalContext:
    _assert_no_future_fields(payload)
    pit = payload.get("pit_assertions") or {}
    if not isinstance(pit, dict) or not pit or not all(pit.values()):
        raise ValueError("historical_replay_pit_assertion_not_clean")
    signal = payload.get("signal") or {}
    deterministic = payload.get("deterministic_decision") or {}
    return SignalContext(
        decision_id=str(payload["source_decision_id"]),
        provider_name="",
        side=str(signal.get("side") or ""),
        symbol=str(signal.get("symbol") or ""),
        entry_low=signal.get("entry_low"),
        entry_high=signal.get("entry_high"),
        stop_loss=signal.get("stop_loss"),
        take_profits=[str(value) for value in (signal.get("take_profits") or [])],
        decision_class=str(deterministic.get("decision_class") or ""),
        decision_reasons=list(deterministic.get("reasons") or []),
        trades_resolved=0,
        provider_evidence_claims=list(payload.get("provider_evidence_claims") or []),
        market_context=(
            dict(payload["market_context"])
            if isinstance(payload.get("market_context"), dict)
            else None
        ),
        recent_messages=list(payload.get("recent_messages") or []),
        self_calibration=None,
        supplemental_evidence=None,
        event_liquidity_execution_context=(
            dict(payload["event_liquidity_execution_context"])
            if isinstance(payload.get("event_liquidity_execution_context"), dict)
            else None
        ),
        provider_alpha_analogue_context=(
            dict(payload["provider_alpha_analogue_context"])
            if isinstance(payload.get("provider_alpha_analogue_context"), dict)
            else None
        ),
        probability_ev_management_context=(
            dict(payload["probability_ev_management_context"])
            if isinstance(payload.get("probability_ev_management_context"), dict)
            else None
        ),
        failure_self_critique_context=(
            dict(payload["failure_self_critique_context"])
            if isinstance(payload.get("failure_self_critique_context"), dict)
            else None
        ),
        preflight_evidence_calls=0,
    )


async def _reason_with_provider_claim_retry(
    engine: AidyReasoningEngine,
    context: SignalContext,
    *,
    retry_limit: int = _PROVIDER_CLAIM_RETRY_LIMIT,
) -> tuple[Any, int]:
    """Retry provider-claim violations fail-closed, never by weakening validation.

    If a model response cannot obey the structured provider-history contract, the one
    bounded retry removes provider history from the model context entirely. The retry can
    still use the signal, PIT market state and Build 2 execution evidence, but it cannot
    cite or paraphrase provider history. This keeps the anti-drift validator unchanged.
    """
    retries = 0
    retry_context = context
    while True:
        try:
            return await engine.reason(retry_context), retries
        except AidyReasoningUnavailable as exc:
            if (
                str(exc) != "aidy_reasoning_provider_claim_invalid"
                or retries >= retry_limit
            ):
                raise
            retries += 1
            retry_context = replace(
                context,
                provider_evidence_claims=[],
                provider_intelligence=None,
                provider_profile=None,
            )


def _shadow_score(
    *, action: str, risk_multiplier: Decimal, actual_pnl_usd: Decimal
) -> tuple[Decimal, Decimal]:
    if action == "take":
        shadow = actual_pnl_usd
    elif action == "reduce":
        if not Decimal("0") < risk_multiplier < Decimal("1"):
            raise ValueError("historical_replay_reduce_multiplier_invalid")
        shadow = actual_pnl_usd * risk_multiplier
    elif action in {"hold", "reject", "need_more_evidence"}:
        shadow = Decimal("0")
    else:
        raise ValueError("historical_replay_action_invalid")
    return shadow, shadow - actual_pnl_usd


@dataclass
class HistoricalReplaySummary:
    cases_materialized: int = 0
    decisions_written: int = 0
    decisions_failed: int = 0
    scores_written: int = 0
    total_replay_delta_usd: Decimal = Decimal("0")
    total_replay_shadow_pnl_usd: Decimal = Decimal("0")
    failures: list[str] = field(default_factory=list)


class AidyHistoricalReplayService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        engine: AidyReasoningEngine,
        scope: str = "development_validation",
        max_calls: int = _DEFAULT_MAX_CALLS,
    ) -> None:
        if scope not in {"development", "development_validation", "holdout"}:
            raise ValueError("historical_replay_scope_invalid")
        self._session_factory = session_factory
        self._engine = engine
        self._scope = scope
        self._max_calls = max_calls

    @staticmethod
    def _profile_and_intelligence(
        candidate: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return AidyReasoningRunner._provider_brain(candidate)

    @staticmethod
    def _market_context(candidate: dict[str, Any]) -> dict[str, Any] | None:
        return AidyReasoningRunner._attached_market_context(candidate)

    def materialize(self, *, limit: int = 500) -> int:
        # Build 5 inherits the exact frozen Build 4 cohort rather than re-querying mutable
        # source tables. Only PIT-safe failure-attribution/self-critique evidence is appended.
        with self._session_factory() as session:
            existing = int(
                session.execute(
                    text(
                        "SELECT count(*) FROM aidy_historical_replay_cases "
                        "WHERE input_contract_version=:input_contract_version"
                    ),
                    {"input_contract_version": INPUT_CONTRACT_VERSION},
                ).scalar_one()
            )
            if existing >= _EXPECTED_FROZEN_CASES:
                return 0
            previous = [
                dict(row)
                for row in session.execute(
                    _LOAD_PREVIOUS_CASES,
                    {
                        "previous_input_contract_version": _PREVIOUS_INPUT_CONTRACT_VERSION,
                        "limit": limit,
                    },
                ).mappings()
            ]

        if len(previous) != _EXPECTED_FROZEN_CASES:
            raise ValueError(
                f"historical_replay_previous_frozen_cohort_incomplete:{len(previous)}"
            )

        written = 0
        for case in previous:
            base = case.get("input_payload")
            if not isinstance(base, dict):
                raise ValueError("historical_replay_previous_payload_not_object")
            signal = base.get("signal")
            if not isinstance(signal, dict):
                raise ValueError("historical_replay_previous_signal_not_object")

            build2 = (
                dict(base["event_liquidity_execution_context"])
                if isinstance(base.get("event_liquidity_execution_context"), dict)
                else None
            )
            build3 = (
                dict(base["provider_alpha_analogue_context"])
                if isinstance(base.get("provider_alpha_analogue_context"), dict)
                else None
            )
            build4 = (
                dict(base["probability_ev_management_context"])
                if isinstance(base.get("probability_ev_management_context"), dict)
                else None
            )
            replay_self_feedback = load_replay_self_feedback(
                self._session_factory,
                signal_posted_at=case["signal_posted_at"],
                source_id=case["source_id"],
            )
            failure_self_critique_context = build_failure_self_critique_context(
                market_context=(
                    dict(base["market_context"])
                    if isinstance(base.get("market_context"), dict)
                    else None
                ),
                build2_context=build2,
                build3_context=build3,
                build4_context=build4,
                self_calibration=None,
                replay_self_feedback=replay_self_feedback,
            )

            payload = dict(base)
            payload["input_contract_version"] = INPUT_CONTRACT_VERSION
            payload["failure_self_critique_context"] = failure_self_critique_context
            provenance = (
                dict(payload["selection_provenance"])
                if isinstance(payload.get("selection_provenance"), dict)
                else {}
            )
            provenance["derived_from_input_contract_version"] = (
                _PREVIOUS_INPUT_CONTRACT_VERSION
            )
            provenance["same_frozen_source_decision"] = True
            payload["selection_provenance"] = provenance
            pit = (
                dict(payload["pit_assertions"])
                if isinstance(payload.get("pit_assertions"), dict)
                else {}
            )
            pit["derived_from_previous_frozen_contract"] = True
            payload["pit_assertions"] = pit

            _assert_no_future_fields(payload)
            if not all(payload["pit_assertions"].values()):
                raise ValueError(
                    f"historical_replay_pit_assertion_failed:{case['source_decision_id']}"
                )

            with self._session_factory() as session:
                result = session.execute(
                    _INSERT_CASE,
                    {
                        "id": str(uuid4()),
                        "source_decision_id": str(case["source_decision_id"]),
                        "source_id": str(case["source_id"]),
                        "signal_posted_at": case["signal_posted_at"],
                        "partition": str(case["partition"]),
                        "input_contract_version": INPUT_CONTRACT_VERSION,
                        "input_payload": _canonical(payload),
                        "input_digest": _digest(payload),
                        "model_eligible": True,
                    },
                )
                session.commit()
                written += int(result.rowcount or 0)
        return written

    def _selected_cases(self, *, limit: int) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            return [
                dict(row)
                for row in session.execute(
                    _SELECT_CASES,
                    {
                        "scope": self._scope,
                        "limit": limit,
                        "replay_version": REPLAY_VERSION,
                        "input_contract_version": INPUT_CONTRACT_VERSION,
                    }
                ).mappings()
            ]

    async def reason(self, *, limit: int = _DEFAULT_BATCH) -> tuple[int, int, list[str]]:
        with self._session_factory() as session:
            already = int(
                session.execute(
                    _COUNT_DECISIONS, {"replay_version": REPLAY_VERSION}
                ).scalar_one()
            )
        remaining = max(0, self._max_calls - already)
        if remaining <= 0:
            return 0, 0, ["historical_replay_max_calls_reached"]

        cases = self._selected_cases(limit=min(limit, remaining))
        written = 0
        failed = 0
        failures: list[str] = []
        for case in cases:
            payload = case["input_payload"]
            if not isinstance(payload, dict):
                failed += 1
                failures.append(f"{case['id']}:payload_not_object")
                continue
            if _digest(payload) != case["input_digest"]:
                failed += 1
                failures.append(f"{case['id']}:input_digest_mismatch")
                continue
            try:
                _assert_no_future_fields(payload)
                pit = payload.get("pit_assertions") or {}
                if not isinstance(pit, dict) or not all(pit.values()):
                    raise ValueError("pit_assertion_not_clean")

                annotation, provider_claim_retries = await _reason_with_provider_claim_retry(
                    self._engine,
                    _signal_context_from_payload(payload),
                )
                output_payload = {
                    "lean": annotation.lean,
                    "confidence": annotation.confidence,
                    "rationale": annotation.rationale,
                    "key_factors": annotation.key_factors,
                    "shadow_action": annotation.shadow_action,
                    "risk_multiplier": annotation.risk_multiplier,
                    "action_reason": annotation.action_reason,
                    "provider_claim_refs": list(annotation.provider_claim_refs),
                    "provider_claim_validation_retries": provider_claim_retries,
                    "outcome_visible_to_model": False,
                    "tools_offered": False,
                }
                with self._session_factory() as session:
                    result = session.execute(
                        _INSERT_DECISION,
                        {
                            "id": str(uuid4()),
                            "case_id": str(case["id"]),
                            "replay_version": REPLAY_VERSION,
                            "model_version": MODEL_VERSION,
                            "prompt_version": PROMPT_VERSION,
                            "model_name": annotation.model_name,
                            "input_digest": case["input_digest"],
                            "output_payload": _canonical(output_payload),
                            "response_id": annotation.response_id,
                            "input_tokens": annotation.input_tokens,
                            "output_tokens": annotation.output_tokens,
                            "estimated_cost_usd": annotation.estimated_cost_usd,
                            "latency_ms": annotation.latency_ms,
                        },
                    )
                    session.commit()
                    written += int(result.rowcount or 0)
            except (AidyReasoningUnavailable, ValueError, TypeError, KeyError) as exc:
                failed += 1
                failures.append(f"{case['id']}:{type(exc).__name__}:{str(exc)[:160]}")
        return written, failed, failures

    def score(self, *, limit: int = 500) -> tuple[int, Decimal, Decimal]:
        with self._session_factory() as session:
            rows = [
                dict(row)
                for row in session.execute(
                    _SELECT_UNSCORED,
                    {"limit": limit, "replay_version": REPLAY_VERSION},
                ).mappings()
            ]

        written = 0
        total_delta = Decimal("0")
        total_shadow = Decimal("0")
        for row in rows:
            output = row.get("output_payload")
            if not isinstance(output, dict):
                continue
            action = str(output.get("shadow_action") or "")
            multiplier = Decimal(str(output.get("risk_multiplier") or 0))
            actual = Decimal(str(row["actual_pnl_usd"]))

            try:
                shadow, delta = _shadow_score(
                    action=action,
                    risk_multiplier=multiplier,
                    actual_pnl_usd=actual,
                )
            except ValueError:
                continue
            with self._session_factory() as session:
                result = session.execute(
                    _INSERT_SCORE,
                    {
                        "id": str(uuid4()),
                        "replay_decision_id": str(row["replay_decision_id"]),
                        "source_decision_id": str(row["source_decision_id"]),
                        "partition": row["partition"],
                        "resolution": row["resolution"],
                        "baseline_pnl_usd": row["baseline_pnl_usd"],
                        "baseline_realized_r": row["baseline_realized_r"],
                        "actual_pnl_usd": row["actual_pnl_usd"],
                        "actual_realized_r": row["actual_realized_r"],
                        "replay_shadow_pnl_usd": shadow,
                        "replay_delta_vs_taken_usd": delta,
                        "outcome_resolved_at": row["resolved_at"],
                    },
                )
                session.commit()
                inserted = int(result.rowcount or 0)
                written += inserted
                if inserted:
                    total_delta += delta
                    total_shadow += shadow
        return written, total_delta, total_shadow

    async def run_once(self, *, batch: int = _DEFAULT_BATCH) -> HistoricalReplaySummary:
        started = datetime.now(UTC)
        summary = HistoricalReplaySummary()
        summary.cases_materialized = await asyncio.to_thread(self.materialize)
        (
            summary.decisions_written,
            summary.decisions_failed,
            summary.failures,
        ) = await self.reason(limit=batch)
        (
            summary.scores_written,
            summary.total_replay_delta_usd,
            summary.total_replay_shadow_pnl_usd,
        ) = await asyncio.to_thread(self.score)

        finished = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                _INSERT_RUN,
                {
                    "id": str(uuid4()),
                    "replay_version": REPLAY_VERSION,
                    "model_version": MODEL_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "partition_scope": self._scope,
                    "cases_materialized": summary.cases_materialized,
                    "decisions_written": summary.decisions_written,
                    "decisions_failed": summary.decisions_failed,
                    "scores_written": summary.scores_written,
                    "total_replay_delta_usd": summary.total_replay_delta_usd,
                    "total_replay_shadow_pnl_usd": summary.total_replay_shadow_pnl_usd,
                    "holdout_opened": self._scope == "holdout",
                    "started_at": started,
                    "finished_at": finished,
                    "details": _canonical({"failures": summary.failures[:25]}),
                },
            )
            session.commit()
        return summary


class AidyHistoricalReplayRuntime:
    """Weekend/background replay loop. Disabled by default and research-only."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        interval_seconds: int | None = None,
        batch: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds or _positive_int(
            "AIDY_HISTORICAL_REPLAY_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        )
        self._batch = batch or _positive_int("AIDY_HISTORICAL_REPLAY_BATCH", _DEFAULT_BATCH)
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _record_runtime_state(self, state: str, *, scope: str) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                _INSERT_RUN,
                {
                    "id": str(uuid4()),
                    "replay_version": REPLAY_VERSION,
                    "model_version": MODEL_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "partition_scope": scope,
                    "cases_materialized": 0,
                    "decisions_written": 0,
                    "decisions_failed": 0,
                    "scores_written": 0,
                    "total_replay_delta_usd": Decimal("0"),
                    "total_replay_shadow_pnl_usd": Decimal("0"),
                    "holdout_opened": scope == "holdout",
                    "started_at": now,
                    "finished_at": now,
                    "details": _canonical({"runtime_state": state}),
                },
            )
            session.commit()

    async def start(self) -> bool:
        if self.running:
            return True
        scope = _scope_from_env()
        if os.getenv("AIDY_HISTORICAL_REPLAY_ENABLED", "0").strip() != "1":
            self._record_runtime_state("disabled_by_configuration", scope=scope)
            logger.info("AIDY historical replay disabled by configuration")
            return False
        api_key = _api_key()
        if not api_key:
            self._record_runtime_state("missing_openai_api_key", scope=scope)
            logger.warning("AIDY historical replay enabled but no OpenAI API key is configured")
            return False

        self._record_runtime_state("started", scope=scope)
        self._stopping.clear()
        service = AidyHistoricalReplayService(
            self._session_factory,
            engine=AidyReasoningEngine(
                api_key=api_key,
                model=os.getenv("AIDY_REASONING_MODEL", "gpt-5-mini-2025-08-07").strip(),
            ),
            scope=scope,
            max_calls=_positive_int("AIDY_HISTORICAL_REPLAY_MAX_CALLS", _DEFAULT_MAX_CALLS),
        )
        self._task = asyncio.create_task(
            self._run(service), name="super-signals-aidy-historical-replay"
        )
        logger.info(
            "AIDY historical replay started scope=%s interval=%ss batch=%s",
            scope,
            self._interval_seconds,
            self._batch,
        )
        return True

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stopping.set()
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self, service: AidyHistoricalReplayService) -> None:
        while not self._stopping.is_set():
            try:
                summary = await service.run_once(batch=self._batch)
                logger.info(
                    "AIDY historical replay materialized=%s written=%s failed=%s scored=%s "
                    "delta=%s shadow_pnl=%s",
                    summary.cases_materialized,
                    summary.decisions_written,
                    summary.decisions_failed,
                    summary.scores_written,
                    summary.total_replay_delta_usd,
                    summary.total_replay_shadow_pnl_usd,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - research replay must never affect the app
                logger.exception("AIDY historical replay failed safely")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue


__all__ = [
    "AidyHistoricalReplayRuntime",
    "AidyHistoricalReplayService",
    "HistoricalReplaySummary",
    "INPUT_CONTRACT_VERSION",
    "REPLAY_VERSION",
    "_assert_no_future_fields",
    "_partition",
    "_reason_with_provider_claim_retry",
    "_scope_from_env",
    "_shadow_score",
    "_signal_context_from_payload",
]
