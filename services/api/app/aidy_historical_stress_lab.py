"""Large reconstructed historical stress lab for current AIDY.

This is deliberately separate from the exact-PIT 140-case Historical Time Machine.

Purpose:
- use hundreds of older, resolved provider trades to stress-test the *current* AIDY;
- reconstruct only information that could have existed by the signal timestamp;
- never expose the target trade's outcome to the model;
- mark retrospective market bars as reconstructed research evidence, never exact PIT;
- preserve the official 18-case sealed holdout untouched.

The lab uses the existing replay case/decision/score tables under separate contract/version ids.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Iterable, Mapping
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_event_liquidity_execution import build_event_liquidity_execution_context
from app.aidy_failure_self_critique import build_failure_self_critique_context
from app.aidy_historical_replay import (
    _assert_no_future_fields,
    _canonical,
    _digest,
    _reason_with_provider_claim_retry,
    _shadow_score,
    _signal_context_from_payload,
)
from app.aidy_market_client import AidyM1Bar, AidyMarketClient
from app.aidy_probability_ev_management import build_probability_ev_management_context
from app.aidy_reasoning_engine import (
    MODEL_VERSION,
    PROMPT_VERSION,
    AidyReasoningEngine,
    AidyReasoningUnavailable,
)

logger = logging.getLogger(__name__)

STRESS_REPLAY_VERSION = "aidy_historical_stress_lab_v1"
STRESS_INPUT_CONTRACT_VERSION = "aidy_historical_stress_input_v1"
STRESS_MARKET_CONTRACT_VERSION = "aidy_historical_stress_market_v1"
STRESS_ANALOGUE_VERSION = "aidy_historical_stress_analogue_v1"

# Reconstructed cohort ends before the exact-PIT cohort begins.
_COHORT_START = datetime(2026, 8, 1, tzinfo=UTC)
_COHORT_END = datetime(2026, 9, 8, 13, 22, 57, tzinfo=UTC)
_TRAIN_END = datetime(2026, 9, 4, tzinfo=UTC)
_VALIDATION_END = datetime(2026, 9, 5, tzinfo=UTC)

_DEFAULT_INTERVAL_SECONDS = 20
_DEFAULT_BATCH = 12
_DEFAULT_MAX_CALLS = 650
_EXPECTED_COHORT = 803
_MARKET_LOOKBACK = timedelta(hours=5)

_CANDIDATES = text(
    """
    WITH ranked AS (
        SELECT
            o.id AS observation_id,
            o.message_id,
            o.source_id,
            o.revision_index,
            o.side,
            o.symbol,
            o.entry_low,
            o.entry_high,
            o.stop_loss,
            o.take_profits,
            d.id AS source_decision_id,
            d.signal_posted_at,
            COALESCE(NULLIF(src.chat_title,''),src.source_alias,'UNKNOWN') AS provider_name,
            ROW_NUMBER() OVER (
                PARTITION BY o.message_id
                ORDER BY o.revision_index ASC,o.observed_at ASC,o.id ASC
            ) AS rn,
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'posted_at',q.posted_at,
                        'text',q.effective_text,
                        'revision_index_as_of_signal',q.effective_revision_index
                    )
                    ORDER BY q.posted_at
                )
                FROM (
                    SELECT m.posted_at,
                           left(COALESCE(r.raw_text,m.raw_text),700) AS effective_text,
                           COALESCE(r.revision_index,0) AS effective_revision_index
                    FROM messages m
                    LEFT JOIN LATERAL (
                        SELECT mr.raw_text,mr.revision_index
                        FROM message_revisions mr
                        WHERE mr.message_id=m.id
                          AND mr.edited_at<=d.signal_posted_at
                        ORDER BY mr.revision_index DESC,mr.edited_at DESC
                        LIMIT 1
                    ) r ON true
                    WHERE m.source_id=o.source_id
                      AND m.posted_at<=d.signal_posted_at
                      AND (m.deleted_at IS NULL OR m.deleted_at>d.signal_posted_at)
                    ORDER BY m.posted_at DESC,m.telegram_message_id DESC
                    LIMIT 5
                ) q
            ) AS recent_messages
        FROM provider_trade_observations o
        JOIN LATERAL (
            SELECT ad.id,ad.signal_posted_at
            FROM aidy_decisions ad
            WHERE ad.observation_id=o.id
              AND ad.decision_class='approve'
            ORDER BY ad.decided_at,ad.id
            LIMIT 1
        ) d ON true
        JOIN LATERAL (
            SELECT ps.id
            FROM provider_trade_scores ps
            WHERE ps.observation_id=o.id
              AND ps.outcome IS NOT NULL
              AND ps.outcome NOT ILIKE '%unresolv%'
            ORDER BY ps.scored_at DESC,ps.id DESC
            LIMIT 1
        ) ps ON true
        JOIN aidy_decision_outcomes ao ON ao.decision_id=d.id
        JOIN sources src ON src.id=o.source_id
        WHERE d.signal_posted_at>=:cohort_start
          AND d.signal_posted_at<:cohort_end
          AND o.decision='new_trade'
          AND o.action='execute'
          AND o.executable IS TRUE
          AND o.update_type IS NULL
          AND upper(COALESCE(o.symbol,'')) IN ('XAUUSD','GOLD')
          AND upper(COALESCE(o.side,'')) IN ('BUY','SELL')
          AND o.stop_loss IS NOT NULL
          AND jsonb_typeof(o.take_profits)='array'
          AND jsonb_array_length(o.take_profits)>0
    )
    SELECT *
    FROM ranked
    WHERE rn=1
    ORDER BY signal_posted_at,source_decision_id
    """
)

_PRIOR_POOL = text(
    """
    WITH ranked AS (
        SELECT
            o.message_id,
            o.source_id,
            o.side,
            d.signal_posted_at,
            ps.last_bar_utc AS prior_result_known_at,
            ps.net_pnl_usd,
            ps.realized_r,
            ps.outcome,
            ROW_NUMBER() OVER (
                PARTITION BY o.message_id
                ORDER BY o.revision_index ASC,o.observed_at ASC,o.id ASC
            ) AS rn
        FROM provider_trade_observations o
        JOIN LATERAL (
            SELECT ad.id,ad.signal_posted_at
            FROM aidy_decisions ad
            WHERE ad.observation_id=o.id
              AND ad.decision_class='approve'
            ORDER BY ad.decided_at,ad.id
            LIMIT 1
        ) d ON true
        JOIN LATERAL (
            SELECT ps.*
            FROM provider_trade_scores ps
            WHERE ps.observation_id=o.id
              AND ps.outcome IS NOT NULL
              AND ps.outcome NOT ILIKE '%unresolv%'
            ORDER BY ps.scored_at DESC,ps.id DESC
            LIMIT 1
        ) ps ON true
        WHERE d.signal_posted_at>=:cohort_start
          AND d.signal_posted_at<:cohort_end
          AND ps.last_bar_utc IS NOT NULL
          AND ps.last_bar_utc<=:cohort_end
          AND upper(COALESCE(o.symbol,'')) IN ('XAUUSD','GOLD')
          AND upper(COALESCE(o.side,'')) IN ('BUY','SELL')
          AND o.decision='new_trade'
          AND o.action='execute'
          AND o.executable IS TRUE
          AND o.update_type IS NULL
          AND o.stop_loss IS NOT NULL
          AND jsonb_typeof(o.take_profits)='array'
          AND jsonb_array_length(o.take_profits)>0
    )
    SELECT *
    FROM ranked
    WHERE rn=1
    ORDER BY signal_posted_at,message_id
    """
)

_INSERT_CASE = text(
    """
    INSERT INTO aidy_historical_replay_cases (
        id,source_decision_id,source_id,signal_posted_at,partition,evidence_tier,
        input_contract_version,input_payload,input_digest,model_eligible,
        research_only,live_money_execution_allowed
    ) VALUES (
        :id,:source_decision_id,:source_id,:signal_posted_at,:partition,'reconstructed_research',
        :input_contract_version,CAST(:input_payload AS jsonb),:input_digest,true,
        true,false
    )
    ON CONFLICT (source_decision_id,input_contract_version) DO NOTHING
    """
)

_SELECT_CASES = text(
    """
    SELECT c.*
    FROM aidy_historical_replay_cases c
    LEFT JOIN aidy_historical_replay_decisions d
      ON d.case_id=c.id AND d.replay_version=:replay_version
    WHERE d.id IS NULL
      AND c.input_contract_version=:input_contract_version
      AND c.model_eligible=true
      AND (
        (:scope='train' AND c.partition='research_train')
        OR (:scope='train_validation' AND c.partition IN ('research_train','research_validation'))
        OR (:scope='validation' AND c.partition='research_validation')
        OR (:scope='oos' AND c.partition='research_oos')
        OR (:scope='all' AND c.partition IN ('research_train','research_validation','research_oos'))
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
    SELECT d.id AS replay_decision_id,d.output_payload,c.source_decision_id,c.partition,
           ao.baseline_pnl_usd,ao.baseline_realized_r,ao.actual_pnl_usd,ao.actual_realized_r,
           ao.resolved_at,ao.resolution
    FROM aidy_historical_replay_decisions d
    JOIN aidy_historical_replay_cases c ON c.id=d.case_id
    JOIN aidy_decision_outcomes ao ON ao.decision_id=c.source_decision_id
    LEFT JOIN aidy_historical_replay_scores s ON s.replay_decision_id=d.id
    WHERE s.id IS NULL
      AND d.replay_version=:replay_version
    ORDER BY d.decided_at,d.id
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
        :total_replay_delta_usd,:total_replay_shadow_pnl_usd,false,
        true,false,:started_at,:finished_at,CAST(:details AS jsonb)
    )
    """
)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _partition(at: datetime) -> str:
    point = at.astimezone(UTC)
    if point < _TRAIN_END:
        return "research_train"
    if point < _VALIDATION_END:
        return "research_validation"
    return "research_oos"


def _session(at: datetime) -> str:
    hour = at.astimezone(UTC).hour
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "london_new_york_overlap"
    if 16 <= hour < 21:
        return "new_york"
    if 21 <= hour < 22:
        return "rollover"
    return "other"


def _bar_close_at_or_before(bars: list[AidyM1Bar], when: datetime) -> Decimal | None:
    eligible = [bar for bar in bars if bar.open_time_utc < when]
    return eligible[-1].close if eligible else None


def _trend_label(
    bars: list[AidyM1Bar],
    *,
    cutoff: datetime,
    minutes: int,
    noise_multiplier: Decimal,
) -> str:
    if not bars:
        return "unknown"
    latest = _bar_close_at_or_before(bars, cutoff)
    prior = _bar_close_at_or_before(bars, cutoff - timedelta(minutes=minutes))
    if latest is None or prior is None:
        return "unknown"
    recent = [bar for bar in bars if cutoff - timedelta(minutes=60) <= bar.open_time_utc < cutoff]
    ranges = [bar.high - bar.low for bar in recent if bar.high >= bar.low]
    if not ranges:
        return "unknown"
    median_range = Decimal(str(statistics.median([float(value) for value in ranges])))
    threshold = max(median_range * noise_multiplier, latest * Decimal("0.00010"))
    delta = latest - prior
    if delta > threshold:
        return "bullish"
    if delta < -threshold:
        return "bearish"
    return "flat"


def _trend_structure(labels: Mapping[str, str]) -> str:
    values = list(labels.values())
    bullish = sum(1 for value in values if value == "bullish")
    bearish = sum(1 for value in values if value == "bearish")
    known = [value for value in values if value != "unknown"]
    if bullish >= 2:
        return "bullish_trend"
    if bearish >= 2:
        return "bearish_trend"
    if known and all(value == "flat" for value in known):
        return "range"
    if len(known) >= 2:
        return "mixed"
    return "unknown"


def _volatility_band(bars: list[AidyM1Bar], *, cutoff: datetime) -> str:
    recent = [
        bar.high - bar.low
        for bar in bars
        if cutoff - timedelta(minutes=30) <= bar.open_time_utc < cutoff
    ]
    baseline = [
        bar.high - bar.low
        for bar in bars
        if cutoff - timedelta(hours=4) <= bar.open_time_utc < cutoff - timedelta(minutes=30)
    ]
    if len(recent) < 10 or len(baseline) < 30:
        return "unknown"
    recent_mean = sum(recent, Decimal("0")) / Decimal(len(recent))
    baseline_mean = sum(baseline, Decimal("0")) / Decimal(len(baseline))
    if baseline_mean <= 0:
        return "unknown"
    ratio = recent_mean / baseline_mean
    if ratio >= Decimal("1.40"):
        return "high"
    if ratio <= Decimal("0.70"):
        return "low"
    return "normal"


def build_reconstructed_market_context(
    *,
    signal_posted_at: datetime,
    bars: list[AidyM1Bar],
) -> dict[str, Any]:
    """Create research-only market context from bars strictly before the signal.

    The bars are fetched retrospectively, so this packet is never labelled exact PIT even
    though the time window itself is cut off at the signal.
    """

    at = signal_posted_at.astimezone(UTC)
    cutoff = at.replace(second=0, microsecond=0)
    eligible = [
        bar
        for bar in bars
        if cutoff - _MARKET_LOOKBACK <= bar.open_time_utc < cutoff
    ]
    eligible.sort(key=lambda item: item.open_time_utc)

    labels = {
        "M15": _trend_label(
            eligible, cutoff=cutoff, minutes=15, noise_multiplier=Decimal("0.5")
        ),
        "H1": _trend_label(
            eligible, cutoff=cutoff, minutes=60, noise_multiplier=Decimal("1.0")
        ),
        "H4": _trend_label(
            eligible, cutoff=cutoff, minutes=240, noise_multiplier=Decimal("2.0")
        ),
    }
    structure = _trend_structure(labels)
    vol = _volatility_band(eligible, cutoff=cutoff)

    last = eligible[-1] if eligible else None
    quote_time = (
        min(cutoff, last.open_time_utc + timedelta(minutes=1))
        if last is not None
        else None
    )
    age = int((at - quote_time).total_seconds()) if quote_time is not None else None
    fresh = age is not None and 0 <= age <= 120

    return {
        "contract_version": STRESS_MARKET_CONTRACT_VERSION,
        "as_of_utc": at.isoformat(),
        "reconstruction_tier": "retrospective_research_m1",
        "pit_eligible": False,
        "decision_admitted": False,
        "session": _session(at),
        "trend_by_timeframe": labels,
        "trend_structure": structure,
        "volatility_band": vol,
        "event_timing": "unknown",
        "quote_state": "known" if last is not None else "unknown",
        "quote_freshness": "fresh" if fresh else ("stale" if last is not None else "unknown"),
        "market": {
            "quote_context": {
                "mid": str(last.close) if last is not None else None,
                "quote_time": quote_time.isoformat() if quote_time else None,
                "quote_state": "known" if last is not None else "unknown",
                "quote_age_seconds": age,
                "spread": None,
            }
        },
        "reconstruction_provenance": {
            "market_source": "aidy_research_m1_retrospective",
            "bars_end_at_or_before_signal": True,
            "future_bars_in_model_input": False,
            "exact_pit_claimed": False,
        },
        "research_only": True,
        "live_money_execution_allowed": False,
    }


def _stress_analogue_context(
    rows: list[dict[str, Any]],
    *,
    source_id: Any,
    side: str,
    signal_posted_at: datetime,
) -> dict[str, Any]:
    target_session = _session(signal_posted_at)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        prior_signal = row.get("signal_posted_at")
        known_at = row.get("prior_result_known_at")
        if not isinstance(prior_signal, datetime) or not isinstance(known_at, datetime):
            continue
        if prior_signal.astimezone(UTC) >= signal_posted_at.astimezone(UTC):
            continue
        if known_at.astimezone(UTC) > signal_posted_at.astimezone(UTC):
            continue
        same_provider = str(row.get("source_id")) == str(source_id)
        same_session = _session(prior_signal) == target_session
        similarity = Decimal("0.5")
        if same_provider:
            similarity += Decimal("0.3")
        if same_session:
            similarity += Decimal("0.2")
        ranked.append(
            (
                float(similarity),
                {
                    "case_id": f"stress-prior:{row.get('message_id')}",
                    "signal_posted_at": prior_signal.astimezone(UTC).isoformat(),
                    "prior_result_known_at": known_at.astimezone(UTC).isoformat(),
                    "same_provider": same_provider,
                    "similarity": float(similarity),
                    "matched_features": [
                        item
                        for item, matched in (
                            ("same_provider", same_provider),
                            ("side", str(row.get("side") or "").upper() == side.upper()),
                            ("session", same_session),
                        )
                        if matched
                    ],
                    "prior_pnl_usd": str(row.get("net_pnl_usd") or 0),
                    "prior_realized_r": (
                        str(row.get("realized_r"))
                        if row.get("realized_r") is not None
                        else None
                    ),
                    "prior_result_class": str(row.get("outcome") or "unknown"),
                },
            )
        )
    ranked.sort(
        key=lambda item: (
            -item[0],
            -int(bool(item[1]["same_provider"])),
            -datetime.fromisoformat(item[1]["signal_posted_at"]).timestamp(),
        )
    )
    top = [item for _, item in ranked[:5]]
    enough = len(top) >= 3
    pnl = [Decimal(str(item["prior_pnl_usd"])) for item in top]
    positive = sum(1 for value in pnl if value > 0)
    mean_pnl = sum(pnl, Decimal("0")) / Decimal(len(pnl)) if pnl else None
    return {
        "contract_version": "aidy_historical_stress_provider_analogue_v1",
        "as_of_utc": signal_posted_at.astimezone(UTC).isoformat(),
        "conditional_alpha": {
            "status": "reconstructed_research_only",
            "statistical_status": "UNKNOWN",
            "threshold_approval_status": "UNKNOWN",
            "usable_as_pretrade_alpha": False,
            "reason": "exact_point_in_time_day13_registry_not_available_for_this_old_trade",
            "research_only": True,
            "live_money_execution_allowed": False,
        },
        "historical_analogue": {
            "analogue_version": STRESS_ANALOGUE_VERSION,
            "status": "descriptive_sample_available" if enough else "insufficient_prior_analogues",
            "sample_n": len(top),
            "minimum_sample_n": 3,
            "positive_outcome_count": positive if enough else None,
            "positive_outcome_rate": round(positive / len(top), 6) if enough and top else None,
            "mean_prior_pnl_usd": str(mean_pnl) if enough and mean_pnl is not None else None,
            "analogues": top,
            "selection_bias_possible": True,
            "descriptive_only": True,
            "usable_for_live_edge_claim": False,
            "all_outcomes_resolved_before_target": True,
            "research_only": True,
            "live_money_execution_allowed": False,
        },
        "research_only": True,
        "live_money_execution_allowed": False,
    }


def _signal(candidate: Mapping[str, Any]) -> dict[str, Any]:
    raw_targets = candidate.get("take_profits")
    targets = raw_targets if isinstance(raw_targets, list) else []
    return {
        "side": str(candidate.get("side") or "").upper(),
        "symbol": "XAUUSD",
        "entry_low": str(candidate.get("entry_low")) if candidate.get("entry_low") is not None else None,
        "entry_high": str(candidate.get("entry_high")) if candidate.get("entry_high") is not None else None,
        "stop_loss": str(candidate.get("stop_loss")) if candidate.get("stop_loss") is not None else None,
        "take_profits": [str(value) for value in targets],
    }


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
    scope = os.getenv("AIDY_HISTORICAL_STRESS_SCOPE", "train").strip()
    return scope if scope in {"train", "validation", "train_validation", "oos", "all"} else "train"


def _api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("Open", "").strip()


@dataclass
class StressLabSummary:
    cases_materialized: int = 0
    decisions_written: int = 0
    decisions_failed: int = 0
    scores_written: int = 0
    total_replay_delta_usd: Decimal = Decimal("0")
    total_replay_shadow_pnl_usd: Decimal = Decimal("0")
    failures: list[str] = field(default_factory=list)


class AidyHistoricalStressLabService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        engine: AidyReasoningEngine,
        market_client: AidyMarketClient,
        scope: str = "train",
        max_calls: int = _DEFAULT_MAX_CALLS,
    ) -> None:
        if scope not in {"train", "validation", "train_validation", "oos", "all"}:
            raise ValueError("historical_stress_scope_invalid")
        self._session_factory = session_factory
        self._engine = engine
        self._market_client = market_client
        self._scope = scope
        self._max_calls = max_calls

    def _candidates(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            return [
                dict(row)
                for row in session.execute(
                    _CANDIDATES,
                    {"cohort_start": _COHORT_START, "cohort_end": _COHORT_END},
                ).mappings()
            ]

    async def _day_bars(self, days: Iterable[datetime]) -> dict[str, list[AidyM1Bar]]:
        output: dict[str, list[AidyM1Bar]] = {}
        unique_days = sorted({point.astimezone(UTC).date() for point in days})
        for day in unique_days:
            start = datetime(day.year, day.month, day.day, tzinfo=UTC) - _MARKET_LOOKBACK
            end = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
            try:
                window = await self._market_client.fetch_research_m1(start=start, end=end)
                output[day.isoformat()] = list(window.bars)
            except Exception as exc:  # noqa: BLE001 - missing research bars should not affect live app
                logger.warning(
                    "AIDY stress lab research M1 unavailable day=%s error=%s",
                    day.isoformat(),
                    type(exc).__name__,
                )
                output[day.isoformat()] = []
        return output

    def _prior_pool(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            return [
                dict(row)
                for row in session.execute(
                    _PRIOR_POOL,
                    {"cohort_start": _COHORT_START, "cohort_end": _COHORT_END},
                ).mappings()
            ]

    @staticmethod
    def _prior_rows(
        pool: list[dict[str, Any]],
        *,
        as_of: datetime,
        source_id: Any,
        side: str,
    ) -> list[dict[str, Any]]:
        eligible = [
            row
            for row in pool
            if str(row.get("side") or "").upper() == side.upper()
            and isinstance(row.get("signal_posted_at"), datetime)
            and isinstance(row.get("prior_result_known_at"), datetime)
            and row["signal_posted_at"].astimezone(UTC) < as_of.astimezone(UTC)
            and row["prior_result_known_at"].astimezone(UTC) <= as_of.astimezone(UTC)
        ]
        eligible.sort(
            key=lambda row: (
                str(row.get("source_id")) != str(source_id),
                -row["signal_posted_at"].astimezone(UTC).timestamp(),
            )
        )
        return eligible[:30]

    async def materialize(self) -> int:
        with self._session_factory() as session:
            existing = int(
                session.execute(
                    text(
                        "SELECT count(*) FROM aidy_historical_replay_cases "
                        "WHERE input_contract_version=:v"
                    ),
                    {"v": STRESS_INPUT_CONTRACT_VERSION},
                ).scalar_one()
            )
        if existing >= _EXPECTED_COHORT:
            return 0

        candidates = self._candidates()
        if len(candidates) != _EXPECTED_COHORT:
            raise ValueError(f"historical_stress_candidate_count_changed:{len(candidates)}")

        day_bars = await self._day_bars(
            [candidate["signal_posted_at"] for candidate in candidates]
        )
        prior_pool = await asyncio.to_thread(self._prior_pool)
        written = 0
        write_session = self._session_factory()
        try:
            for index, candidate in enumerate(candidates, start=1):
                at = candidate["signal_posted_at"].astimezone(UTC)
                signal = _signal(candidate)
                bars = day_bars.get(at.date().isoformat(), [])
                market = build_reconstructed_market_context(
                    signal_posted_at=at,
                    bars=bars,
                )
                build2 = build_event_liquidity_execution_context(
                    signal_posted_at=at,
                    side=signal["side"],
                    entry_low=signal["entry_low"],
                    entry_high=signal["entry_high"],
                    stop_loss=signal["stop_loss"],
                    take_profits=signal["take_profits"],
                    market_context=market,
                    execution_calibration=None,
                )
                prior_rows = self._prior_rows(
                    prior_pool,
                    as_of=at,
                    source_id=candidate["source_id"],
                    side=signal["side"],
                )
                build3 = _stress_analogue_context(
                    prior_rows,
                    source_id=candidate["source_id"],
                    side=signal["side"],
                    signal_posted_at=at,
                )
                build4 = build_probability_ev_management_context(
                    signal=signal,
                    build2_context=build2,
                    build3_context=build3,
                )
                build5 = build_failure_self_critique_context(
                    market_context=market,
                    build2_context=build2,
                    build3_context=build3,
                    build4_context=build4,
                    self_calibration=None,
                    replay_self_feedback=None,
                )
                analogues = build3["historical_analogue"]["analogues"]
                no_future_analogues = all(
                    datetime.fromisoformat(item["prior_result_known_at"]).astimezone(UTC) <= at
                    for item in analogues
                )
                payload = {
                    "input_contract_version": STRESS_INPUT_CONTRACT_VERSION,
                    "source_decision_id": str(candidate["source_decision_id"]),
                    "source_id": str(candidate["source_id"]),
                    "provider_name": str(candidate.get("provider_name") or ""),
                    "signal_posted_at": at.isoformat(),
                    "signal": signal,
                    "deterministic_decision": {
                        "decision_class": "approve",
                        "reasons": ["historical_stress_executable_provider_trade"],
                    },
                    "provider_evidence_claims": [],
                    "market_context": market,
                    "recent_messages": list(candidate.get("recent_messages") or []),
                    "event_liquidity_execution_context": build2,
                    "provider_alpha_analogue_context": build3,
                    "probability_ev_management_context": build4,
                    "failure_self_critique_context": build5,
                    "selection_provenance": {
                        "cohort": "older_executable_resolved_unique_provider_messages",
                        "cohort_start_utc": _COHORT_START.isoformat(),
                        "cohort_end_utc_exclusive": _COHORT_END.isoformat(),
                        "message_deduplication": "earliest_executable_revision_per_message",
                        "market_evidence_tier": "retrospective_research_m1",
                        "exact_pit_claimed": False,
                        "official_18_case_holdout_excluded": True,
                    },
                    "pit_assertions": {
                        "target_outcome_excluded_from_model_input": True,
                        "research_market_window_ends_at_or_before_signal": True,
                        "prior_analogue_results_known_at_or_before_signal": no_future_analogues,
                        "retrospective_market_source_explicitly_tagged": True,
                        "official_exact_pit_holdout_excluded": True,
                        "research_only": True,
                    },
                }
                _assert_no_future_fields(payload)
                if not all(payload["pit_assertions"].values()):
                    raise ValueError(
                        f"historical_stress_time_boundary_failed:{candidate['source_decision_id']}"
                    )

                result = write_session.execute(
                    _INSERT_CASE,
                    {
                        "id": str(uuid4()),
                        "source_decision_id": str(candidate["source_decision_id"]),
                        "source_id": str(candidate["source_id"]),
                        "signal_posted_at": at,
                        "partition": _partition(at),
                        "input_contract_version": STRESS_INPUT_CONTRACT_VERSION,
                        "input_payload": _canonical(payload),
                        "input_digest": _digest(payload),
                    },
                )
                written += int(result.rowcount or 0)
                if index % 100 == 0:
                    write_session.commit()
            write_session.commit()
        finally:
            write_session.close()
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
                        "replay_version": STRESS_REPLAY_VERSION,
                        "input_contract_version": STRESS_INPUT_CONTRACT_VERSION,
                    },
                ).mappings()
            ]

    async def reason(self, *, limit: int = _DEFAULT_BATCH) -> tuple[int, int, list[str]]:
        with self._session_factory() as session:
            already = int(
                session.execute(
                    text(
                        "SELECT count(*) FROM aidy_historical_replay_decisions "
                        "WHERE replay_version=:v"
                    ),
                    {"v": STRESS_REPLAY_VERSION},
                ).scalar_one()
            )
        remaining = max(0, self._max_calls - already)
        if remaining <= 0:
            return 0, 0, ["historical_stress_max_calls_reached"]

        cases = self._selected_cases(limit=min(limit, remaining))
        written = 0
        failed = 0
        failures: list[str] = []
        for case in cases:
            payload = case.get("input_payload")
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
                assertions = payload.get("pit_assertions") or {}
                if not isinstance(assertions, dict) or not all(assertions.values()):
                    raise ValueError("historical_stress_assertion_not_clean")
                annotation, retries = await _reason_with_provider_claim_retry(
                    self._engine,
                    _signal_context_from_payload(payload),
                )
                output = {
                    "lean": annotation.lean,
                    "confidence": annotation.confidence,
                    "rationale": annotation.rationale,
                    "key_factors": annotation.key_factors,
                    "shadow_action": annotation.shadow_action,
                    "risk_multiplier": annotation.risk_multiplier,
                    "action_reason": annotation.action_reason,
                    "action_calibration": annotation.action_calibration,
                    "provider_claim_refs": list(annotation.provider_claim_refs),
                    "provider_claim_validation_retries": retries,
                    "outcome_visible_to_model": False,
                    "tools_offered": False,
                    "evidence_tier": "reconstructed_research",
                }
                with self._session_factory() as session:
                    result = session.execute(
                        _INSERT_DECISION,
                        {
                            "id": str(uuid4()),
                            "case_id": str(case["id"]),
                            "replay_version": STRESS_REPLAY_VERSION,
                            "model_version": MODEL_VERSION,
                            "prompt_version": PROMPT_VERSION,
                            "model_name": annotation.model_name,
                            "input_digest": case["input_digest"],
                            "output_payload": _canonical(output),
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
                failures.append(f"{case['id']}:{type(exc).__name__}:{str(exc)[:180]}")
        return written, failed, failures

    def score(self, *, limit: int = 1000) -> tuple[int, Decimal, Decimal]:
        with self._session_factory() as session:
            rows = [
                dict(row)
                for row in session.execute(
                    _SELECT_UNSCORED,
                    {"replay_version": STRESS_REPLAY_VERSION, "limit": limit},
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

    async def run_once(self, *, batch: int = _DEFAULT_BATCH) -> StressLabSummary:
        started = datetime.now(UTC)
        summary = StressLabSummary()
        summary.cases_materialized = await self.materialize()
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
                    "replay_version": STRESS_REPLAY_VERSION,
                    "model_version": MODEL_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "partition_scope": f"stress_{self._scope}",
                    "cases_materialized": summary.cases_materialized,
                    "decisions_written": summary.decisions_written,
                    "decisions_failed": summary.decisions_failed,
                    "scores_written": summary.scores_written,
                    "total_replay_delta_usd": summary.total_replay_delta_usd,
                    "total_replay_shadow_pnl_usd": summary.total_replay_shadow_pnl_usd,
                    "started_at": started,
                    "finished_at": finished,
                    "details": _canonical({"failures": summary.failures[:25]}),
                },
            )
            session.commit()
        return summary


class AidyHistoricalStressLabRuntime:
    """Background research stress lab. Disabled by default and never live-authoritative."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        interval_seconds: int | None = None,
        batch: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds or _positive_int(
            "AIDY_HISTORICAL_STRESS_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        )
        self._batch = batch or _positive_int(
            "AIDY_HISTORICAL_STRESS_BATCH", _DEFAULT_BATCH
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.running:
            return True
        if os.getenv("AIDY_HISTORICAL_STRESS_ENABLED", "0").strip() != "1":
            logger.info("AIDY historical stress lab disabled by configuration")
            return False
        api_key = _api_key()
        market_client = AidyMarketClient.from_environment()
        if not api_key or market_client is None:
            logger.warning("AIDY historical stress lab missing API/model market configuration")
            return False
        scope = _scope_from_env()
        service = AidyHistoricalStressLabService(
            self._session_factory,
            engine=AidyReasoningEngine(
                api_key=api_key,
                model=os.getenv("AIDY_REASONING_MODEL", "gpt-5-mini-2025-08-07").strip(),
            ),
            market_client=market_client,
            scope=scope,
            max_calls=_positive_int(
                "AIDY_HISTORICAL_STRESS_MAX_CALLS", _DEFAULT_MAX_CALLS
            ),
        )
        self._stopping.clear()
        self._task = asyncio.create_task(
            self._run(service),
            name="super-signals-aidy-historical-stress-lab",
        )
        logger.info(
            "AIDY historical stress lab started scope=%s interval=%ss batch=%s",
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

    async def _run(self, service: AidyHistoricalStressLabService) -> None:
        while not self._stopping.is_set():
            try:
                summary = await service.run_once(batch=self._batch)
                logger.info(
                    "AIDY stress lab materialized=%s written=%s failed=%s scored=%s "
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
            except Exception:  # noqa: BLE001 - research lab must never affect production execution
                logger.exception("AIDY historical stress lab failed safely")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue


__all__ = [
    "AidyHistoricalStressLabRuntime",
    "AidyHistoricalStressLabService",
    "STRESS_INPUT_CONTRACT_VERSION",
    "STRESS_REPLAY_VERSION",
    "build_reconstructed_market_context",
    "_partition",
]
