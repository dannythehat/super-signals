"""Provider Intelligence Day 12 dormant fingerprint/statistics harness.

Only forward/PIT score-eligible closed paper evidence is admitted. The harness is
research-only, has no broker/execution imports, and can never grant statistical or
live-money authority. Raw rates are descriptive; posterior partial-pooled estimates
are the primary surface.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import fmean, variance
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text

from app.db import get_engine, get_session_factory

MODEL_VERSION = "provider_day12_v1"
MINIMUM_FORWARD_N = 30
STATISTICAL_STATUS = "WAITING-FOR-FORWARD-EVIDENCE"
PRIMARY_ESTIMATE_KIND = "posterior_partial_pool"
_PRIOR_STRENGTH = 8.0
_Z95 = 1.959963984540054


@dataclass(frozen=True, slots=True)
class FingerprintObservation:
    trade_id: UUID
    source_id: UUID
    side: str
    session_bucket: str
    duration_seconds: float
    mae_r: float
    mfe_r: float
    target_hits: tuple[tuple[int, bool], ...]
    stop_hit: bool
    break_even: bool


def _bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _binary_posterior(*, successes: int, n: int, population_successes: int, population_n: int) -> dict[str, float | int | str]:
    if n <= 0:
        raise ValueError("binary_posterior_requires_nonzero_n")
    population_rate = (population_successes + 0.5) / (population_n + 1.0) if population_n else 0.5
    alpha0 = max(0.001, population_rate * _PRIOR_STRENGTH)
    beta0 = max(0.001, (1.0 - population_rate) * _PRIOR_STRENGTH)
    alpha = alpha0 + successes
    beta = beta0 + (n - successes)
    total = alpha + beta
    mean = alpha / total
    sd = math.sqrt((alpha * beta) / ((total * total) * (total + 1.0)))
    return {
        "model": "empirical_bayes_beta_binomial",
        "population_rate": round(population_rate, 8),
        "posterior_mean": round(mean, 8),
        "credible_lower_95": round(_bounded(mean - _Z95 * sd), 8),
        "credible_upper_95": round(_bounded(mean + _Z95 * sd), 8),
        "successes": successes,
        "n": n,
    }


def _continuous_posterior(*, values: list[float], population_values: list[float], nonnegative: bool = True) -> dict[str, float | int | str]:
    if not values:
        raise ValueError("continuous_posterior_requires_nonzero_n")
    pop = population_values or values
    pop_mean = fmean(pop)
    pop_var = variance(pop) if len(pop) > 1 else max(abs(pop_mean) * 0.25, 1.0) ** 2
    pop_var = max(pop_var, 1e-9)
    sample_mean = fmean(values)
    sample_var = variance(values) if len(values) > 1 else pop_var
    sample_var = max(sample_var, pop_var * 0.05, 1e-9)
    prior_precision = 1.0 / pop_var
    data_precision = len(values) / sample_var
    posterior_var = 1.0 / (prior_precision + data_precision)
    posterior_mean = posterior_var * (prior_precision * pop_mean + data_precision * sample_mean)
    posterior_sd = math.sqrt(posterior_var)
    lower = posterior_mean - _Z95 * posterior_sd
    upper = posterior_mean + _Z95 * posterior_sd
    if nonnegative:
        lower = max(0.0, lower)
    return {
        "model": "empirical_bayes_normal_partial_pool",
        "population_mean": round(pop_mean, 8),
        "posterior_mean": round(posterior_mean, 8),
        "credible_lower_95": round(lower, 8),
        "credible_upper_95": round(upper, 8),
        "n": len(values),
    }


def _dimension_rows(observations: list[FingerprintObservation]) -> list[tuple[str, UUID, str | None, str | None, list[FingerprintObservation]]]:
    groups: dict[tuple[str, UUID, str | None, str | None], list[FingerprintObservation]] = {}
    for obs in observations:
        keys = (
            ("provider", obs.source_id, None, None),
            ("provider_direction", obs.source_id, obs.side, None),
            ("provider_session", obs.source_id, None, obs.session_bucket),
            ("provider_direction_session", obs.source_id, obs.side, obs.session_bucket),
        )
        for key in keys:
            groups.setdefault(key, []).append(obs)
    return [(*key, groups[key]) for key in sorted(groups, key=lambda k: (str(k[1]), k[0], k[2] or "", k[3] or ""))]


def build_fingerprint_cells(observations: Iterable[FingerprintObservation]) -> list[dict[str, Any]]:
    obs = sorted(list(observations), key=lambda row: (str(row.source_id), str(row.trade_id)))
    if not obs:
        return []

    population_duration = [row.duration_seconds for row in obs]
    population_mae = [row.mae_r for row in obs]
    population_mfe = [row.mfe_r for row in obs]
    population_stop = sum(row.stop_hit for row in obs)
    population_be = sum(row.break_even for row in obs)
    target_indexes = sorted({index for row in obs for index, _ in row.target_hits})
    population_targets: dict[int, tuple[int, int]] = {}
    for index in target_indexes:
        opportunities = [(hit) for row in obs for idx, hit in row.target_hits if idx == index]
        population_targets[index] = (sum(opportunities), len(opportunities))

    cells: list[dict[str, Any]] = []
    for dimension_kind, source_id, side, session_bucket, rows in _dimension_rows(obs):
        n = len(rows)
        duration = [row.duration_seconds for row in rows]
        mae = [row.mae_r for row in rows]
        mfe = [row.mfe_r for row in rows]
        stop_count = sum(row.stop_hit for row in rows)
        be_count = sum(row.break_even for row in rows)
        target_posteriors: dict[str, dict[str, Any]] = {}
        target_descriptive: dict[str, dict[str, Any]] = {}
        for index in target_indexes:
            opportunities = [hit for row in rows for idx, hit in row.target_hits if idx == index]
            if not opportunities:
                continue
            successes = sum(opportunities)
            pop_successes, pop_n = population_targets[index]
            target_posteriors[str(index)] = _binary_posterior(
                successes=successes,
                n=len(opportunities),
                population_successes=pop_successes,
                population_n=pop_n,
            )
            target_descriptive[str(index)] = {
                "hits": successes,
                "opportunities": len(opportunities),
                "raw_hit_rate_descriptive_only": round(successes / len(opportunities), 8),
            }

        posterior = {
            "primary_estimate_kind": PRIMARY_ESTIMATE_KIND,
            "tp_hit_rates": target_posteriors,
            "stop_rate": _binary_posterior(successes=stop_count, n=n, population_successes=population_stop, population_n=len(obs)),
            "break_even_rate": _binary_posterior(successes=be_count, n=n, population_successes=population_be, population_n=len(obs)),
            "duration_seconds": _continuous_posterior(values=duration, population_values=population_duration),
            "mae_r": _continuous_posterior(values=mae, population_values=population_mae),
            "mfe_r": _continuous_posterior(values=mfe, population_values=population_mfe),
        }
        descriptive = {
            "raw_values_are_descriptive_only": True,
            "tp": target_descriptive,
            "stop_count": stop_count,
            "break_even_count": be_count,
            "raw_stop_rate_descriptive_only": round(stop_count / n, 8),
            "raw_break_even_rate_descriptive_only": round(be_count / n, 8),
            "raw_mean_duration_seconds_descriptive_only": round(fmean(duration), 8),
            "raw_mean_mae_r_descriptive_only": round(fmean(mae), 8),
            "raw_mean_mfe_r_descriptive_only": round(fmean(mfe), 8),
        }
        cells.append(
            {
                "dimension_kind": dimension_kind,
                "source_id": source_id,
                "side": side,
                "session_bucket": session_bucket,
                "sample_count": n,
                "n_gate_met": n >= MINIMUM_FORWARD_N,
                "posterior": posterior,
                "descriptive": descriptive,
                "statistical_status": STATISTICAL_STATUS,
            }
        )
    return cells


def _float(value: Any) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)))


def _load_observations(session: Any, *, cutoff: datetime) -> list[FingerprintObservation]:
    rows = session.execute(
        text(
            """
            SELECT t.id,t.source_id,t.side,t.session_bucket,t.opened_at,t.closed_at,
                   t.entry_price,t.initial_stop,t.max_price,t.min_price,t.quality_r_multiple,
                   COALESCE(jsonb_agg(jsonb_build_object('tp_index',l.tp_index,'exit_reason',l.exit_reason))
                     FILTER (WHERE l.id IS NOT NULL),'[]'::jsonb) AS legs
            FROM shadow_trades t
            JOIN sources s ON s.id=t.source_id
            LEFT JOIN shadow_trade_legs l ON l.shadow_trade_id=t.id
            WHERE s.status IN ('shadow','testing')
              AND t.status='closed' AND t.closed_at IS NOT NULL AND t.closed_at<=:cutoff
              AND t.score_eligible
              AND t.provider_profile_pit_status='RESOLVED'
              AND t.entry_price IS NOT NULL AND t.initial_stop IS NOT NULL
              AND t.max_price IS NOT NULL AND t.min_price IS NOT NULL
              AND t.opened_at IS NOT NULL
            GROUP BY t.id
            ORDER BY t.source_id,t.closed_at,t.id
            """
        ),
        {"cutoff": cutoff},
    ).mappings().all()

    observations: list[FingerprintObservation] = []
    for row in rows:
        entry = _float(row["entry_price"])
        stop = _float(row["initial_stop"])
        high = _float(row["max_price"])
        low = _float(row["min_price"])
        if None in {entry, stop, high, low}:
            continue
        assert entry is not None and stop is not None and high is not None and low is not None
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        if row["side"] == "BUY":
            mae_r = max(0.0, (entry - low) / risk)
            mfe_r = max(0.0, (high - entry) / risk)
        elif row["side"] == "SELL":
            mae_r = max(0.0, (high - entry) / risk)
            mfe_r = max(0.0, (entry - low) / risk)
        else:
            continue
        legs = row["legs"] if isinstance(row["legs"], list) else []
        target_hits = tuple(
            sorted(
                (int(leg["tp_index"]), str(leg.get("exit_reason") or "") == "target")
                for leg in legs
                if leg.get("tp_index") is not None
            )
        )
        quality_r = _float(row["quality_r_multiple"]) or 0.0
        explicit_be = any(str(leg.get("exit_reason") or "").casefold() in {"be", "break_even", "breakeven", "shadow_break_even"} for leg in legs)
        break_even = explicit_be or abs(quality_r) <= 1e-9
        stop_hit = (not break_even) and any(str(leg.get("exit_reason") or "") == "shadow_stop" for leg in legs)
        duration = max(0.0, (row["closed_at"] - row["opened_at"]).total_seconds())
        observations.append(
            FingerprintObservation(
                trade_id=UUID(str(row["id"])),
                source_id=UUID(str(row["source_id"])),
                side=str(row["side"]),
                session_bucket=str(row["session_bucket"] or "unknown"),
                duration_seconds=duration,
                mae_r=mae_r,
                mfe_r=mfe_r,
                target_hits=target_hits,
                stop_hit=stop_hit,
                break_even=break_even,
            )
        )
    return observations


def _evidence_digest(observations: list[FingerprintObservation]) -> str:
    payload = [
        {
            "trade_id": str(row.trade_id),
            "source_id": str(row.source_id),
            "side": row.side,
            "session": row.session_bucket,
            "duration": round(row.duration_seconds, 6),
            "mae_r": round(row.mae_r, 8),
            "mfe_r": round(row.mfe_r, 8),
            "targets": row.target_hits,
            "stop": row.stop_hit,
            "be": row.break_even,
        }
        for row in observations
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _code_sha() -> str:
    return (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "unknown")[:40]


def run() -> dict[str, Any]:
    session_factory = get_session_factory()
    code_sha = _code_sha()
    cutoff = datetime.now(UTC)
    lock = get_engine().connect()
    acquired = bool(lock.execute(text("SELECT pg_try_advisory_lock(hashtext('provider_day12_fingerprint_v1'))")).scalar_one())
    if not acquired:
        lock.close()
        return {"engineering_status": "SKIPPED_LOCK_HELD", "statistical_status": STATISTICAL_STATUS, "research_only": True, "live_money_execution_allowed": False}
    try:
        with session_factory() as session:
            existing = session.execute(
                text("SELECT id,engineering_status,statistical_status,evidence_digest FROM provider_fingerprint_runs WHERE model_version=:model AND code_sha=:sha AND completed_at IS NOT NULL LIMIT 1"),
                {"model": MODEL_VERSION, "sha": code_sha},
            ).mappings().first()
            if existing:
                return {"run_id": str(existing["id"]), "engineering_status": existing["engineering_status"], "statistical_status": existing["statistical_status"], "evidence_digest": existing["evidence_digest"], "skipped_existing_code_sha": True, "research_only": True, "live_money_execution_allowed": False}

            provider_count = int(session.execute(text("SELECT count(*) FROM sources WHERE status IN ('shadow','testing')")).scalar_one())
            observations = _load_observations(session, cutoff=cutoff)
            cells = build_fingerprint_cells(observations)
            digest = _evidence_digest(observations)
            run_id = session.execute(
                text("""INSERT INTO provider_fingerprint_runs(model_version,code_sha,evidence_cutoff,minimum_forward_n,provider_count,eligible_trade_count,cell_count,evidence_digest) VALUES (:model,:sha,:cutoff,:min_n,:providers,:trades,:cells,:digest) RETURNING id"""),
                {"model": MODEL_VERSION, "sha": code_sha, "cutoff": cutoff, "min_n": MINIMUM_FORWARD_N, "providers": provider_count, "trades": len(observations), "cells": len(cells), "digest": digest},
            ).scalar_one()
            for cell in cells:
                session.execute(
                    text("""INSERT INTO provider_fingerprint_cells(run_id,source_id,dimension_kind,side,session_bucket,sample_count,n_gate_met,primary_estimate_kind,posterior_json,descriptive_json,statistical_status) VALUES (:run_id,:source_id,:dimension_kind,:side,:session_bucket,:sample_count,:n_gate_met,:primary,CAST(:posterior AS jsonb),CAST(:descriptive AS jsonb),:statistical)"""),
                    {"run_id": run_id, "source_id": cell["source_id"], "dimension_kind": cell["dimension_kind"], "side": cell["side"], "session_bucket": cell["session_bucket"], "sample_count": cell["sample_count"], "n_gate_met": cell["n_gate_met"], "primary": PRIMARY_ESTIMATE_KIND, "posterior": json.dumps(cell["posterior"], sort_keys=True), "descriptive": json.dumps(cell["descriptive"], sort_keys=True), "statistical": STATISTICAL_STATUS},
                )
            session.execute(text("UPDATE provider_fingerprint_runs SET completed_at=now(),engineering_status='ENGINEERING_PROVEN',statistical_status=:status WHERE id=:run_id"), {"status": STATISTICAL_STATUS, "run_id": run_id})
            session.commit()
            summary = {
                "run_id": str(run_id),
                "model_version": MODEL_VERSION,
                "engineering_status": "ENGINEERING_PROVEN",
                "statistical_status": STATISTICAL_STATUS,
                "minimum_forward_n": MINIMUM_FORWARD_N,
                "provider_count": provider_count,
                "eligible_trade_count": len(observations),
                "cell_count": len(cells),
                "evidence_digest": digest,
                "primary_estimate_kind": PRIMARY_ESTIMATE_KIND,
                "research_only": True,
                "live_money_execution_allowed": False,
            }
            print("PROVIDER_DAY12_FINGERPRINT=" + json.dumps(summary, sort_keys=True), flush=True)
            return summary
    except Exception as exc:
        raise RuntimeError(f"provider_day12_fingerprint_failed:{type(exc).__name__}:{exc}") from exc
    finally:
        try:
            lock.execute(text("SELECT pg_advisory_unlock(hashtext('provider_day12_fingerprint_v1'))"))
        finally:
            lock.close()


if __name__ == "__main__":
    run()
