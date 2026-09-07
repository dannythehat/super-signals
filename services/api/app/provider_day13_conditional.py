"""Provider Intelligence Day 13 conditional fingerprint/FDR harness.

The Day 13 contract is deliberately research-only. Hypotheses are frozen before
confirmatory observations, real evidence must be OOS relative to preregistration,
and builder-proposed statistical thresholds cannot grant provider/live-money authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean, variance
from typing import Any, Iterable, Mapping
from uuid import UUID

from sqlalchemy import text

from app.db import get_engine, get_session_factory

MODEL_VERSION = "provider_day13_v1"
REGISTRY_VERSION = "provider_day13_preregistered_v1"
AIDY_REGIME_DEFINITION_VERSION = "aidy_gold_regime_v1"
STATISTICAL_STATUS = "WAITING-FOR-FORWARD-EVIDENCE"
THRESHOLD_APPROVAL_STATUS = "PROPOSED_UNAPPROVED"
PRIMARY_ESTIMATE_KIND = "posterior_partial_pool"
PRIMARY_METRIC = "quality_r_multiple"
FDR_FAMILY = "provider_day13_v1_global"
MINIMUM_OOS_N = 30
PROPOSED_FDR_Q = 0.05
PROPOSED_MIN_EFFECT_R = 0.25
SIMULATION_SEED = 13013

SIDES = ("BUY", "SELL")
SESSIONS = ("asia", "europe", "ny_early", "other")
DURATION_BUCKETS = ("lt_15m", "15m_to_60m", "1h_to_4h", "gte_4h")
DURATION_SEMANTICS = "realized_descriptive_post_entry"
REGIME_VALUES: dict[str, tuple[str, ...]] = {
    "trend_structure": ("bullish_trend", "bearish_trend", "range", "mixed"),
    "volatility_band": ("low", "normal", "high"),
    "quote_spread_condition": (
        "stale_quote",
        "fresh_quote_spread_known",
        "fresh_quote_spread_unknown",
    ),
    "event_timing": ("inside_high_impact_window", "clear_current_window"),
}
_Z95 = 1.959963984540054


@dataclass(frozen=True, slots=True)
class RegisteredHypothesis:
    id: UUID
    source_id: UUID
    side: str
    session_bucket: str
    regime_dimension: str
    regime_value: str
    duration_bucket: str
    preregistered_at: datetime
    preregistration_digest: str

    @property
    def key(self) -> tuple[UUID, str, str, str, str, str]:
        return (
            self.source_id,
            self.side,
            self.session_bucket,
            self.regime_dimension,
            self.regime_value,
            self.duration_bucket,
        )

    @property
    def base_key(self) -> tuple[UUID, str, str, str, str]:
        return (
            self.source_id,
            self.side,
            self.session_bucket,
            self.regime_dimension,
            self.duration_bucket,
        )


@dataclass(frozen=True, slots=True)
class ConditionalObservation:
    trade_id: UUID
    source_id: UUID
    signal_id: UUID
    side: str
    session_bucket: str
    signal_posted_at: datetime
    duration_bucket: str
    realized_r: float
    regime_labels: tuple[tuple[str, str], ...]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def duration_bucket(duration_seconds: float) -> str:
    """Fixed preregistered realized-duration bins; never outcome-optimized."""
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise ValueError("duration_seconds_must_be_finite_nonnegative")
    if duration_seconds < 15 * 60:
        return "lt_15m"
    if duration_seconds < 60 * 60:
        return "15m_to_60m"
    if duration_seconds < 4 * 60 * 60:
        return "1h_to_4h"
    return "gte_4h"


def benjamini_hochberg(p_values: Mapping[str, float], *, q: float) -> dict[str, dict[str, float | bool]]:
    """Deterministic global Benjamini-Hochberg adjustment."""
    if not 0 < q <= 1:
        raise ValueError("fdr_q_out_of_range")
    if not p_values:
        return {}
    for key, value in p_values.items():
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"invalid_p_value:{key}")

    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    m = len(ordered)
    adjusted: dict[str, float] = {}
    running = 1.0
    for zero_index in range(m - 1, -1, -1):
        key, value = ordered[zero_index]
        rank = zero_index + 1
        running = min(running, value * m / rank, 1.0)
        adjusted[key] = running
    return {
        key: {"adjusted_p": round(adjusted[key], 12), "rejected": adjusted[key] <= q}
        for key in sorted(adjusted)
    }


def _two_sample_normal_p_value(cell: list[float], complement: list[float]) -> float:
    """Two-sided normal approximation for preregistered confirmatory screening."""
    if len(cell) < 2 or len(complement) < 2:
        raise ValueError("two_sample_test_requires_two_values_per_group")
    cell_var = max(variance(cell), 0.0)
    complement_var = max(variance(complement), 0.0)
    effect = fmean(cell) - fmean(complement)
    standard_error = math.sqrt(cell_var / len(cell) + complement_var / len(complement))
    if standard_error <= 1e-15:
        return 0.0 if abs(effect) > 1e-15 else 1.0
    z_score = abs(effect) / standard_error
    return max(0.0, min(1.0, math.erfc(z_score / math.sqrt(2.0))))


def _partial_pool_mean(values: list[float], population: list[float]) -> dict[str, float | int | str]:
    if not values:
        raise ValueError("partial_pool_requires_nonempty_values")
    pool = population or values
    pool_mean = fmean(pool)
    pool_var = variance(pool) if len(pool) > 1 else max(abs(pool_mean) * 0.25, 1.0) ** 2
    pool_var = max(pool_var, 1e-9)
    sample_mean = fmean(values)
    sample_var = variance(values) if len(values) > 1 else pool_var
    sample_var = max(sample_var, pool_var * 0.05, 1e-9)
    prior_precision = 1.0 / pool_var
    data_precision = len(values) / sample_var
    posterior_var = 1.0 / (prior_precision + data_precision)
    posterior_mean = posterior_var * (prior_precision * pool_mean + data_precision * sample_mean)
    posterior_sd = math.sqrt(posterior_var)
    return {
        "model": "empirical_bayes_normal_partial_pool",
        "posterior_mean_r": round(posterior_mean, 8),
        "credible_lower_95_r": round(posterior_mean - _Z95 * posterior_sd, 8),
        "credible_upper_95_r": round(posterior_mean + _Z95 * posterior_sd, 8),
        "population_mean_r": round(pool_mean, 8),
        "raw_mean_r_descriptive_only": round(sample_mean, 8),
        "n": len(values),
    }


def _evaluate_values(cell: list[float], complement: list[float]) -> dict[str, Any]:
    pool = list(cell) + list(complement)
    cell_posterior = _partial_pool_mean(cell, pool) if cell else None
    complement_posterior = _partial_pool_mean(complement, pool) if complement else None
    raw_cell = fmean(cell) if cell else None
    raw_complement = fmean(complement) if complement else None
    raw_effect = None if raw_cell is None or raw_complement is None else raw_cell - raw_complement
    shrunken_effect = None
    if cell_posterior is not None and complement_posterior is not None:
        shrunken_effect = float(cell_posterior["posterior_mean_r"]) - float(complement_posterior["posterior_mean_r"])
    n_gate = len(cell) >= MINIMUM_OOS_N and len(complement) >= MINIMUM_OOS_N
    p_value = _two_sample_normal_p_value(cell, complement) if n_gate else None
    effect_gate = shrunken_effect is not None and abs(shrunken_effect) >= PROPOSED_MIN_EFFECT_R
    return {
        "cell_oos_n": len(cell),
        "complement_oos_n": len(complement),
        "raw_cell_mean_r": raw_cell,
        "raw_complement_mean_r": raw_complement,
        "raw_effect_r": raw_effect,
        "shrunken_effect_r": shrunken_effect,
        "p_value": p_value,
        "minimum_oos_gate_met": n_gate,
        "minimum_effect_gate_met": effect_gate,
        "posterior": {
            "primary_estimate_kind": PRIMARY_ESTIMATE_KIND,
            "cell": cell_posterior,
            "matched_complement": complement_posterior,
            "shrunken_effect_r": None if shrunken_effect is None else round(shrunken_effect, 8),
        },
        "descriptive": {
            "raw_values_are_descriptive_only": True,
            "raw_cell_mean_r": None if raw_cell is None else round(raw_cell, 8),
            "raw_complement_mean_r": None if raw_complement is None else round(raw_complement, 8),
            "raw_effect_r": None if raw_effect is None else round(raw_effect, 8),
            "comparison": "same_provider_direction_session_duration_regime_dimension_other_known_value",
            "duration_semantics": DURATION_SEMANTICS,
        },
    }


def _simulate_family(*, seed: int, signal_effect_r: float | None) -> dict[str, Any]:
    rng = random.Random(seed)
    evaluations: dict[str, dict[str, Any]] = {}
    p_values: dict[str, float] = {}
    for index in range(80):
        complement = [rng.gauss(0.0, 0.6) for _ in range(60)]
        shift = signal_effect_r if index == 0 and signal_effect_r is not None else 0.0
        cell = [rng.gauss(shift, 0.6) for _ in range(60)]
        evaluation = _evaluate_values(cell, complement)
        key = f"sim_{index:03d}"
        evaluations[key] = evaluation
        assert evaluation["p_value"] is not None
        p_values[key] = float(evaluation["p_value"])
    bh = benjamini_hochberg(p_values, q=PROPOSED_FDR_Q)
    candidates = [
        key
        for key, evaluation in evaluations.items()
        if bool(bh[key]["rejected"]) and bool(evaluation["minimum_effect_gate_met"])
    ]
    return {
        "hypothesis_count": len(evaluations),
        "builder_gate_candidate_count": len(candidates),
        "candidate_keys": candidates,
    }


def simulated_governance_acceptance() -> dict[str, Any]:
    """Known-signal and pure-noise engineering acceptance; never real evidence."""
    known_signal = _simulate_family(seed=SIMULATION_SEED, signal_effect_r=0.8)
    pure_noise = _simulate_family(seed=SIMULATION_SEED, signal_effect_r=None)
    passed = "sim_000" in known_signal["candidate_keys"] and pure_noise["builder_gate_candidate_count"] == 0
    return {
        "simulation_seed": SIMULATION_SEED,
        "synthetic_only": True,
        "known_signal": known_signal,
        "pure_noise": pure_noise,
        "acceptance_passed": passed,
        "statistical_authority_granted": False,
    }


def _preregistration_payload(*, source_id: UUID, side: str, session_bucket: str, regime_dimension: str, regime_value: str, duration: str, preregistered_at: datetime) -> dict[str, Any]:
    return {
        "registry_version": REGISTRY_VERSION,
        "source_id": str(source_id),
        "side": side,
        "session_bucket": session_bucket,
        "regime_definition_version": AIDY_REGIME_DEFINITION_VERSION,
        "regime_dimension": regime_dimension,
        "regime_value": regime_value,
        "duration_bucket": duration,
        "duration_semantics": DURATION_SEMANTICS,
        "primary_metric": PRIMARY_METRIC,
        "minimum_oos_n": MINIMUM_OOS_N,
        "proposed_fdr_q": PROPOSED_FDR_Q,
        "proposed_min_effect_r": PROPOSED_MIN_EFFECT_R,
        "threshold_approval_status": THRESHOLD_APPROVAL_STATUS,
        "oos_boundary": "preregistered_at",
        "preregistered_at": preregistered_at.isoformat(),
        "research_only": True,
        "live_money_execution_allowed": False,
    }


def _ensure_preregistry(session: Any, *, preregistered_at: datetime) -> int:
    source_ids = [
        UUID(str(row[0]))
        for row in session.execute(text("SELECT id FROM sources WHERE status='shadow' ORDER BY id")).all()
    ]
    rows: list[dict[str, Any]] = []
    for source_id in source_ids:
        for side in SIDES:
            for session_bucket in SESSIONS:
                for duration in DURATION_BUCKETS:
                    for regime_dimension, values in REGIME_VALUES.items():
                        for regime_value in values:
                            payload = _preregistration_payload(
                                source_id=source_id,
                                side=side,
                                session_bucket=session_bucket,
                                regime_dimension=regime_dimension,
                                regime_value=regime_value,
                                duration=duration,
                                preregistered_at=preregistered_at,
                            )
                            rows.append({**payload, "preregistration_digest": _digest(payload)})
    if rows:
        session.execute(
            text(
                """
                INSERT INTO provider_conditional_hypotheses(
                    registry_version,source_id,side,session_bucket,regime_definition_version,
                    regime_dimension,regime_value,duration_bucket,duration_semantics,primary_metric,
                    minimum_oos_n,proposed_fdr_q,proposed_min_effect_r,threshold_approval_status,
                    oos_boundary,preregistered_at,preregistration_digest,research_only,
                    live_money_execution_allowed
                ) VALUES (
                    :registry_version,:source_id,:side,:session_bucket,:regime_definition_version,
                    :regime_dimension,:regime_value,:duration_bucket,:duration_semantics,:primary_metric,
                    :minimum_oos_n,:proposed_fdr_q,:proposed_min_effect_r,:threshold_approval_status,
                    :oos_boundary,:preregistered_at,:preregistration_digest,:research_only,
                    :live_money_execution_allowed
                )
                ON CONFLICT (
                    registry_version,source_id,side,session_bucket,regime_dimension,regime_value,duration_bucket
                ) DO NOTHING
                """
            ),
            rows,
        )
    return len(source_ids)


def _load_registry(session: Any) -> list[RegisteredHypothesis]:
    rows = session.execute(
        text(
            """
            SELECT h.id,h.source_id,h.side,h.session_bucket,h.regime_dimension,h.regime_value,
                   h.duration_bucket,h.preregistered_at,h.preregistration_digest
            FROM provider_conditional_hypotheses h
            JOIN sources s ON s.id=h.source_id
            WHERE h.registry_version=:registry_version AND s.status='shadow'
            ORDER BY h.source_id,h.side,h.session_bucket,h.regime_dimension,h.regime_value,h.duration_bucket,h.id
            """
        ),
        {"registry_version": REGISTRY_VERSION},
    ).mappings().all()
    return [
        RegisteredHypothesis(
            id=UUID(str(row["id"])),
            source_id=UUID(str(row["source_id"])),
            side=str(row["side"]),
            session_bucket=str(row["session_bucket"]),
            regime_dimension=str(row["regime_dimension"]),
            regime_value=str(row["regime_value"]),
            duration_bucket=str(row["duration_bucket"]),
            preregistered_at=row["preregistered_at"].astimezone(UTC),
            preregistration_digest=str(row["preregistration_digest"]),
        )
        for row in rows
    ]


def _known_regime_labels(regime_json: object) -> tuple[tuple[str, str], ...]:
    regime = regime_json if isinstance(regime_json, Mapping) else {}
    if regime.get("regime_definition_version") != AIDY_REGIME_DEFINITION_VERSION:
        return ()
    labels = regime.get("labels")
    labels = labels if isinstance(labels, Mapping) else {}
    known: list[tuple[str, str]] = []
    for dimension, allowed in REGIME_VALUES.items():
        value = str(labels.get(dimension) or "unknown")
        if value in allowed:
            known.append((dimension, value))
    return tuple(known)


def _load_observations(session: Any, *, cutoff: datetime) -> list[ConditionalObservation]:
    rows = session.execute(
        text(
            """
            SELECT t.id AS trade_id,t.source_id,t.signal_id,t.side,t.session_bucket,
                   t.opened_at,t.closed_at,t.quality_r_multiple,
                   a.signal_posted_at,a.context_as_of_utc,a.provider_profile_effective_at,a.regime_json
            FROM shadow_trades t
            JOIN sources s ON s.id=t.source_id
            JOIN provider_signal_context_attachments a
              ON a.source_id=t.source_id AND a.signal_id=t.signal_id
            WHERE s.status='shadow'
              AND t.status='closed' AND t.closed_at IS NOT NULL AND t.closed_at<=:cutoff
              AND t.score_eligible
              AND t.provider_profile_pit_status='resolved'
              AND t.opened_at IS NOT NULL
              AND t.quality_r_multiple IS NOT NULL
              AND t.side IN ('BUY','SELL')
              AND t.session_bucket IN ('asia','europe','ny_early','other')
              AND a.signal_posted_at<=:cutoff
              AND a.context_as_of_utc<=a.signal_posted_at
              AND a.provider_profile_effective_at<=a.signal_posted_at
              AND a.regime_json->>'regime_definition_version'=:regime_version
            ORDER BY t.source_id,a.signal_posted_at,t.id
            """
        ),
        {"cutoff": cutoff, "regime_version": AIDY_REGIME_DEFINITION_VERSION},
    ).mappings().all()

    observations: list[ConditionalObservation] = []
    for row in rows:
        duration_seconds = (row["closed_at"] - row["opened_at"]).total_seconds()
        if duration_seconds < 0:
            continue
        labels = _known_regime_labels(row["regime_json"])
        if not labels:
            continue
        observations.append(
            ConditionalObservation(
                trade_id=UUID(str(row["trade_id"])),
                source_id=UUID(str(row["source_id"])),
                signal_id=UUID(str(row["signal_id"])),
                side=str(row["side"]),
                session_bucket=str(row["session_bucket"]),
                signal_posted_at=row["signal_posted_at"].astimezone(UTC),
                duration_bucket=duration_bucket(duration_seconds),
                realized_r=float(row["quality_r_multiple"]),
                regime_labels=labels,
            )
        )
    return observations


def build_conditional_results(
    hypotheses: Iterable[RegisteredHypothesis], observations: Iterable[ConditionalObservation]
) -> tuple[list[dict[str, Any]], set[UUID]]:
    registry = list(hypotheses)
    obs = list(observations)
    if not registry or not obs:
        return [], set()

    exact_registry = {hypothesis.key: hypothesis for hypothesis in registry}
    by_base: dict[tuple[UUID, str, str, str, str], list[tuple[str, float, datetime, UUID]]] = {}
    eligible_trade_ids: set[UUID] = set()
    for observation in obs:
        for regime_dimension, regime_value in observation.regime_labels:
            exact_key = (
                observation.source_id,
                observation.side,
                observation.session_bucket,
                regime_dimension,
                regime_value,
                observation.duration_bucket,
            )
            hypothesis = exact_registry.get(exact_key)
            if hypothesis is None or observation.signal_posted_at <= hypothesis.preregistered_at:
                continue
            eligible_trade_ids.add(observation.trade_id)
            by_base.setdefault(hypothesis.base_key, []).append(
                (regime_value, observation.realized_r, observation.signal_posted_at, observation.trade_id)
            )

    results: list[dict[str, Any]] = []
    p_values: dict[str, float] = {}
    for hypothesis in registry:
        rows = by_base.get(hypothesis.base_key, [])
        if not rows:
            continue
        post_oos = [row for row in rows if row[2] > hypothesis.preregistered_at]
        cell = [row[1] for row in post_oos if row[0] == hypothesis.regime_value]
        if not cell:
            continue
        complement = [row[1] for row in post_oos if row[0] != hypothesis.regime_value]
        evaluation = _evaluate_values(cell, complement)
        result = {
            "hypothesis_id": hypothesis.id,
            "source_id": hypothesis.source_id,
            **evaluation,
            "bh_adjusted_p": None,
            "bh_rejected": False,
            "builder_gate_candidate": False,
            "authoritative_discovery": False,
            "threshold_approval_status": THRESHOLD_APPROVAL_STATUS,
            "statistical_status": STATISTICAL_STATUS,
        }
        results.append(result)
        if evaluation["p_value"] is not None:
            p_values[str(hypothesis.id)] = float(evaluation["p_value"])

    bh = benjamini_hochberg(p_values, q=PROPOSED_FDR_Q)
    for result in results:
        key = str(result["hypothesis_id"])
        adjusted = bh.get(key)
        if adjusted is not None:
            result["bh_adjusted_p"] = float(adjusted["adjusted_p"])
            result["bh_rejected"] = bool(adjusted["rejected"])
        result["builder_gate_candidate"] = bool(
            result["minimum_oos_gate_met"]
            and result["minimum_effect_gate_met"]
            and result["bh_rejected"]
        )
    return results, eligible_trade_ids


def _evidence_digest(hypotheses: list[RegisteredHypothesis], observations: list[ConditionalObservation], eligible_trade_ids: set[UUID]) -> str:
    payload = {
        "registry_version": REGISTRY_VERSION,
        "threshold_approval_status": THRESHOLD_APPROVAL_STATUS,
        "minimum_oos_n": MINIMUM_OOS_N,
        "proposed_fdr_q": PROPOSED_FDR_Q,
        "proposed_min_effect_r": PROPOSED_MIN_EFFECT_R,
        "preregistration_digests": sorted(row.preregistration_digest for row in hypotheses),
        "eligible_observations": [
            {
                "trade_id": str(row.trade_id),
                "source_id": str(row.source_id),
                "signal_id": str(row.signal_id),
                "side": row.side,
                "session": row.session_bucket,
                "signal_posted_at": row.signal_posted_at.isoformat(),
                "duration_bucket": row.duration_bucket,
                "realized_r": round(row.realized_r, 8),
                "regime_labels": row.regime_labels,
            }
            for row in observations
            if row.trade_id in eligible_trade_ids
        ],
    }
    return _digest(payload)


def _code_sha() -> str:
    return (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "unknown")[:40]


def run() -> dict[str, Any]:
    session_factory = get_session_factory()
    code_sha = _code_sha()
    cutoff = datetime.now(UTC)
    lock = get_engine().connect()
    acquired = bool(lock.execute(text("SELECT pg_try_advisory_lock(hashtext('provider_day13_conditional_v1'))")).scalar_one())
    if not acquired:
        lock.close()
        return {
            "engineering_status": "SKIPPED_LOCK_HELD",
            "statistical_status": STATISTICAL_STATUS,
            "threshold_approval_status": THRESHOLD_APPROVAL_STATUS,
            "research_only": True,
            "live_money_execution_allowed": False,
        }
    try:
        with session_factory() as session:
            provider_count = _ensure_preregistry(session, preregistered_at=cutoff)
            session.commit()
            hypotheses = _load_registry(session)

            existing = session.execute(
                text(
                    "SELECT id,engineering_status,statistical_status,evidence_digest FROM provider_conditional_runs "
                    "WHERE model_version=:model AND code_sha=:sha AND completed_at IS NOT NULL LIMIT 1"
                ),
                {"model": MODEL_VERSION, "sha": code_sha},
            ).mappings().first()
            if existing:
                return {
                    "run_id": str(existing["id"]),
                    "engineering_status": existing["engineering_status"],
                    "statistical_status": existing["statistical_status"],
                    "threshold_approval_status": THRESHOLD_APPROVAL_STATUS,
                    "evidence_digest": existing["evidence_digest"],
                    "skipped_existing_code_sha": True,
                    "research_only": True,
                    "live_money_execution_allowed": False,
                }

            observations = _load_observations(session, cutoff=cutoff)
            results, eligible_trade_ids = build_conditional_results(hypotheses, observations)
            simulation = simulated_governance_acceptance()
            engineering_status = "ENGINEERING_PROVEN" if simulation["acceptance_passed"] else "FAILED_SIMULATION_ACCEPTANCE"
            digest = _evidence_digest(hypotheses, observations, eligible_trade_ids)
            tested_count = sum(result["p_value"] is not None for result in results)
            bh_rejected_count = sum(bool(result["bh_rejected"]) for result in results)
            candidate_count = sum(bool(result["builder_gate_candidate"]) for result in results)

            run_id = session.execute(
                text(
                    """
                    INSERT INTO provider_conditional_runs(
                        model_version,registry_version,code_sha,evidence_cutoff,minimum_oos_n,
                        proposed_fdr_q,proposed_min_effect_r,threshold_approval_status,fdr_family,
                        provider_count,preregistered_hypothesis_count,eligible_oos_trade_count,
                        result_count,tested_hypothesis_count,bh_rejected_count,builder_gate_candidate_count,
                        authoritative_discovery_count,simulation_json,evidence_digest
                    ) VALUES (
                        :model,:registry,:sha,:cutoff,:min_n,:fdr_q,:min_effect,:approval,:family,
                        :providers,:hypotheses,:eligible,:results,:tested,:rejected,:candidates,0,
                        CAST(:simulation AS jsonb),:digest
                    ) RETURNING id
                    """
                ),
                {
                    "model": MODEL_VERSION,
                    "registry": REGISTRY_VERSION,
                    "sha": code_sha,
                    "cutoff": cutoff,
                    "min_n": MINIMUM_OOS_N,
                    "fdr_q": PROPOSED_FDR_Q,
                    "min_effect": PROPOSED_MIN_EFFECT_R,
                    "approval": THRESHOLD_APPROVAL_STATUS,
                    "family": FDR_FAMILY,
                    "providers": provider_count,
                    "hypotheses": len(hypotheses),
                    "eligible": len(eligible_trade_ids),
                    "results": len(results),
                    "tested": tested_count,
                    "rejected": bh_rejected_count,
                    "candidates": candidate_count,
                    "simulation": json.dumps(simulation, sort_keys=True),
                    "digest": digest,
                },
            ).scalar_one()

            for result in results:
                session.execute(
                    text(
                        """
                        INSERT INTO provider_conditional_results(
                            run_id,hypothesis_id,source_id,cell_oos_n,complement_oos_n,
                            raw_cell_mean_r,raw_complement_mean_r,raw_effect_r,shrunken_effect_r,
                            p_value,bh_adjusted_p,bh_rejected,minimum_oos_gate_met,
                            minimum_effect_gate_met,builder_gate_candidate,authoritative_discovery,
                            posterior_json,descriptive_json,threshold_approval_status,statistical_status
                        ) VALUES (
                            :run_id,:hypothesis_id,:source_id,:cell_n,:complement_n,
                            :raw_cell,:raw_complement,:raw_effect,:shrunken_effect,:p_value,
                            :bh_adjusted,:bh_rejected,:n_gate,:effect_gate,:candidate,false,
                            CAST(:posterior AS jsonb),CAST(:descriptive AS jsonb),:approval,:status
                        )
                        """
                    ),
                    {
                        "run_id": run_id,
                        "hypothesis_id": result["hypothesis_id"],
                        "source_id": result["source_id"],
                        "cell_n": result["cell_oos_n"],
                        "complement_n": result["complement_oos_n"],
                        "raw_cell": result["raw_cell_mean_r"],
                        "raw_complement": result["raw_complement_mean_r"],
                        "raw_effect": result["raw_effect_r"],
                        "shrunken_effect": result["shrunken_effect_r"],
                        "p_value": result["p_value"],
                        "bh_adjusted": result["bh_adjusted_p"],
                        "bh_rejected": result["bh_rejected"],
                        "n_gate": result["minimum_oos_gate_met"],
                        "effect_gate": result["minimum_effect_gate_met"],
                        "candidate": result["builder_gate_candidate"],
                        "posterior": json.dumps(result["posterior"], sort_keys=True),
                        "descriptive": json.dumps(result["descriptive"], sort_keys=True),
                        "approval": THRESHOLD_APPROVAL_STATUS,
                        "status": STATISTICAL_STATUS,
                    },
                )

            session.execute(
                text(
                    "UPDATE provider_conditional_runs SET completed_at=now(),engineering_status=:engineering_status,"
                    "statistical_status=:statistical_status WHERE id=:run_id"
                ),
                {
                    "engineering_status": engineering_status,
                    "statistical_status": STATISTICAL_STATUS,
                    "run_id": run_id,
                },
            )
            session.commit()

            summary = {
                "run_id": str(run_id),
                "model_version": MODEL_VERSION,
                "registry_version": REGISTRY_VERSION,
                "engineering_status": engineering_status,
                "statistical_status": STATISTICAL_STATUS,
                "threshold_approval_status": THRESHOLD_APPROVAL_STATUS,
                "minimum_oos_n": MINIMUM_OOS_N,
                "proposed_fdr_q": PROPOSED_FDR_Q,
                "proposed_min_effect_r": PROPOSED_MIN_EFFECT_R,
                "fdr_family": FDR_FAMILY,
                "provider_count": provider_count,
                "preregistered_hypothesis_count": len(hypotheses),
                "eligible_oos_trade_count": len(eligible_trade_ids),
                "result_count": len(results),
                "tested_hypothesis_count": tested_count,
                "bh_rejected_count": bh_rejected_count,
                "builder_gate_candidate_count": candidate_count,
                "authoritative_discovery_count": 0,
                "simulation_acceptance_passed": bool(simulation["acceptance_passed"]),
                "evidence_digest": digest,
                "primary_estimate_kind": PRIMARY_ESTIMATE_KIND,
                "research_only": True,
                "live_money_execution_allowed": False,
            }
            print("PROVIDER_DAY13_CONDITIONAL=" + json.dumps(summary, sort_keys=True), flush=True)
            return summary
    finally:
        try:
            lock.execute(text("SELECT pg_advisory_unlock(hashtext('provider_day13_conditional_v1'))"))
        finally:
            lock.close()


if __name__ == "__main__":
    run()
