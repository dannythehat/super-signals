"""Provider Intelligence Day 12 dormant fingerprint/statistics harness.

Only forward/PIT score-eligible CLOSED PAPER evidence from the 40 shadow discovery
providers is admitted. Raw rates are descriptive; hierarchical posterior estimates
are primary. This module has no broker/execution authority and statistical authority
is hard-walled to WAITING-FOR-FORWARD-EVIDENCE.
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


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))


def _binary_posterior(*, successes: int, n: int, population_successes: int, population_n: int) -> dict[str, Any]:
    if n <= 0:
        raise ValueError("binary_posterior_requires_nonzero_n")
    pop_rate = (population_successes + 0.5) / (population_n + 1.0) if population_n else 0.5
    alpha = max(0.001, pop_rate * _PRIOR_STRENGTH) + successes
    beta = max(0.001, (1.0 - pop_rate) * _PRIOR_STRENGTH) + (n - successes)
    total = alpha + beta
    mean = alpha / total
    sd = math.sqrt((alpha * beta) / ((total * total) * (total + 1.0)))
    return {
        "model": "empirical_bayes_beta_binomial",
        "population_rate": round(pop_rate, 8),
        "posterior_mean": round(mean, 8),
        "credible_lower_95": round(_bounded(mean - _Z95 * sd), 8),
        "credible_upper_95": round(_bounded(mean + _Z95 * sd), 8),
        "successes": successes,
        "n": n,
    }


def _continuous_posterior(*, values: list[float], population_values: list[float]) -> dict[str, Any]:
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
    return {
        "model": "empirical_bayes_normal_partial_pool",
        "population_mean": round(pop_mean, 8),
        "posterior_mean": round(posterior_mean, 8),
        "credible_lower_95": round(max(0.0, posterior_mean - _Z95 * posterior_sd), 8),
        "credible_upper_95": round(posterior_mean + _Z95 * posterior_sd, 8),
        "n": len(values),
    }


def _dimension_rows(observations: list[FingerprintObservation]) -> list[tuple[str, UUID, str | None, str | None, list[FingerprintObservation]]]:
    groups: dict[tuple[str, UUID, str | None, str | None], list[FingerprintObservation]] = {}
    for obs in observations:
        for key in (
            ("provider", obs.source_id, None, None),
            ("provider_direction", obs.source_id, obs.side, None),
            ("provider_session", obs.source_id, None, obs.session_bucket),
            ("provider_direction_session", obs.source_id, obs.side, obs.session_bucket),
        ):
            groups.setdefault(key, []).append(obs)
    ordered = sorted(groups, key=lambda key: (str(key[1]), key[0], key[2] or "", key[3] or ""))
    return [(*key, groups[key]) for key in ordered]


def build_fingerprint_cells(observations: Iterable[FingerprintObservation]) -> list[dict[str, Any]]:
    obs = sorted(list(observations), key=lambda row: (str(row.source_id), str(row.trade_id)))
    if not obs:
        return []
    pop_duration = [row.duration_seconds for row in obs]
    pop_mae = [row.mae_r for row in obs]
    pop_mfe = [row.mfe_r for row in obs]
    pop_stop = sum(row.stop_hit for row in obs)
    pop_be = sum(row.break_even for row in obs)
    target_indexes = sorted({idx for row in obs for idx, _ in row.target_hits})
    pop_targets: dict[int, tuple[int, int]] = {}
    for idx in target_indexes:
        hits = [hit for row in obs for target_idx, hit in row.target_hits if target_idx == idx]
        pop_targets[idx] = (sum(hits), len(hits))

    cells: list[dict[str, Any]] = []
    for kind, source_id, side, session_bucket, rows in _dimension_rows(obs):
        n = len(rows)
        stop_count = sum(row.stop_hit for row in rows)
        be_count = sum(row.break_even for row in rows)
        posterior_targets: dict[str, Any] = {}
        descriptive_targets: dict[str, Any] = {}
        for idx in target_indexes:
            hits = [hit for row in rows for target_idx, hit in row.target_hits if target_idx == idx]
            if not hits:
                continue
            successes = sum(hits)
            pop_successes, pop_n = pop_targets[idx]
            posterior_targets[str(idx)] = _binary_posterior(
                successes=successes, n=len(hits), population_successes=pop_successes, population_n=pop_n
            )
            descriptive_targets[str(idx)] = {
                "hits": successes,
                "opportunities": len(hits),
                "raw_hit_rate_descriptive_only": round(successes / len(hits), 8),
            }
        duration = [row.duration_seconds for row in rows]
        mae = [row.mae_r for row in rows]
        mfe = [row.mfe_r for row in rows]
        cells.append({
            "dimension_kind": kind,
            "source_id": source_id,
            "side": side,
            "session_bucket": session_bucket,
            "sample_count": n,
            "n_gate_met": n >= MINIMUM_FORWARD_N,
            "posterior": {
                "primary_estimate_kind": PRIMARY_ESTIMATE_KIND,
                "tp_hit_rates": posterior_targets,
                "stop_rate": _binary_posterior(successes=stop_count, n=n, population_successes=pop_stop, population_n=len(obs)),
                "break_even_rate": _binary_posterior(successes=be_count, n=n, population_successes=pop_be, population_n=len(obs)),
                "duration_seconds": _continuous_posterior(values=duration, population_values=pop_duration),
                "mae_r": _continuous_posterior(values=mae, population_values=pop_mae),
                "mfe_r": _continuous_posterior(values=mfe, population_values=pop_mfe),
            },
            "descriptive": {
                "raw_values_are_descriptive_only": True,
                "tp": descriptive_targets,
                "stop_count": stop_count,
                "break_even_count": be_count,
                "raw_stop_rate_descriptive_only": round(stop_count / n, 8),
                "raw_break_even_rate_descriptive_only": round(be_count / n, 8),
                "raw_mean_duration_seconds_descriptive_only": round(fmean(duration), 8),
                "raw_mean_mae_r_descriptive_only": round(fmean(mae), 8),
                "raw_mean_mfe_r_descriptive_only": round(fmean(mfe), 8),
            },
            "statistical_status": STATISTICAL_STATUS,
        })
    return cells


def _float(value: Any) -> float | None:
    return None if value is None else float(Decimal(str(value)))


def _load_observations(session: Any, *, cutoff: datetime) -> list[FingerprintObservation]:
    rows = session.execute(text("""
        SELECT t.id,t.source_id,t.side,t.session_bucket,t.opened_at,t.closed_at,
               t.entry_price,t.initial_stop,t.max_price,t.min_price,t.quality_r_multiple,
               COALESCE(jsonb_agg(jsonb_build_object('tp_index',l.tp_index,'exit_reason',l.exit_reason))
                 FILTER (WHERE l.id IS NOT NULL),'[]'::jsonb) AS legs
        FROM shadow_trades t
        JOIN sources s ON s.id=t.source_id
        LEFT JOIN shadow_trade_legs l ON l.shadow_trade_id=t.id
        WHERE s.status='shadow'
          AND t.status='closed' AND t.closed_at IS NOT NULL AND t.closed_at<=:cutoff
          AND t.score_eligible
          AND t.provider_profile_pit_status='resolved'
          AND t.entry_price IS NOT NULL AND t.initial_stop IS NOT NULL
          AND t.max_price IS NOT NULL AND t.min_price IS NOT NULL AND t.opened_at IS NOT NULL
        GROUP BY t.id ORDER BY t.source_id,t.closed_at,t.id
    """), {"cutoff": cutoff}).mappings().all()

    observations: list[FingerprintObservation] = []
    for row in rows:
        entry, stop, high, low = (_float(row[name]) for name in ("entry_price", "initial_stop", "max_price", "min_price"))
        if entry is None or stop is None or high is None or low is None:
            continue
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        if row["side"] == "BUY":
            mae_r, mfe_r = max(0.0, (entry - low) / risk), max(0.0, (high - entry) / risk)
        elif row["side"] == "SELL":
            mae_r, mfe_r = max(0.0, (high - entry) / risk), max(0.0, (entry - low) / risk)
        else:
            continue
        legs = row["legs"] if isinstance(row["legs"], list) else []
        target_hits = tuple(sorted(
            (int(leg["tp_index"]), str(leg.get("exit_reason") or "") == "target")
            for leg in legs if leg.get("tp_index") is not None
        ))
        quality_r = _float(row["quality_r_multiple"]) or 0.0
        explicit_be = any(str(leg.get("exit_reason") or "").casefold() in {"be","break_even","breakeven","shadow_break_even"} for leg in legs)
        break_even = explicit_be or abs(quality_r) <= 1e-9
        stop_hit = (not break_even) and any(str(leg.get("exit_reason") or "") == "shadow_stop" for leg in legs)
        observations.append(FingerprintObservation(
            trade_id=UUID(str(row["id"])), source_id=UUID(str(row["source_id"])), side=str(row["side"]),
            session_bucket=str(row["session_bucket"] or "unknown"),
            duration_seconds=max(0.0, (row["closed_at"] - row["opened_at"]).total_seconds()),
            mae_r=mae_r, mfe_r=mfe_r, target_hits=target_hits, stop_hit=stop_hit, break_even=break_even,
        ))
    return observations


def _evidence_digest(observations: list[FingerprintObservation]) -> str:
    payload = [
        (str(row.trade_id), str(row.source_id), row.side, row.session_bucket, round(row.duration_seconds, 6),
         round(row.mae_r, 8), round(row.mfe_r, 8), row.target_hits, row.stop_hit, row.break_even)
        for row in observations
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _code_sha() -> str:
    return (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "unknown")[:40]


def run() -> dict[str, Any]:
    session_factory = get_session_factory()
    code_sha, cutoff = _code_sha(), datetime.now(UTC)
    lock = get_engine().connect()
    acquired = bool(lock.execute(text("SELECT pg_try_advisory_lock(hashtext('provider_day12_fingerprint_v1'))")).scalar_one())
    if not acquired:
        lock.close()
        return {"engineering_status":"SKIPPED_LOCK_HELD","statistical_status":STATISTICAL_STATUS,"research_only":True,"live_money_execution_allowed":False}
    try:
        with session_factory() as session:
            existing = session.execute(text("SELECT id,engineering_status,statistical_status,evidence_digest FROM provider_fingerprint_runs WHERE model_version=:model AND code_sha=:sha AND completed_at IS NOT NULL LIMIT 1"), {"model":MODEL_VERSION,"sha":code_sha}).mappings().first()
            if existing:
                return {"run_id":str(existing["id"]),"engineering_status":existing["engineering_status"],"statistical_status":existing["statistical_status"],"evidence_digest":existing["evidence_digest"],"skipped_existing_code_sha":True,"research_only":True,"live_money_execution_allowed":False}
            provider_count = int(session.execute(text("SELECT count(*) FROM sources WHERE status='shadow'")).scalar_one())
            observations = _load_observations(session, cutoff=cutoff)
            cells = build_fingerprint_cells(observations)
            digest = _evidence_digest(observations)
            run_id = session.execute(text("""INSERT INTO provider_fingerprint_runs(model_version,code_sha,evidence_cutoff,minimum_forward_n,provider_count,eligible_trade_count,cell_count,evidence_digest) VALUES (:model,:sha,:cutoff,:min_n,:providers,:trades,:cells,:digest) RETURNING id"""), {"model":MODEL_VERSION,"sha":code_sha,"cutoff":cutoff,"min_n":MINIMUM_FORWARD_N,"providers":provider_count,"trades":len(observations),"cells":len(cells),"digest":digest}).scalar_one()
            for cell in cells:
                session.execute(text("""INSERT INTO provider_fingerprint_cells(run_id,source_id,dimension_kind,side,session_bucket,sample_count,n_gate_met,primary_estimate_kind,posterior_json,descriptive_json,statistical_status) VALUES (:run_id,:source_id,:kind,:side,:session,:n,:gate,:primary,CAST(:posterior AS jsonb),CAST(:descriptive AS jsonb),:status)"""), {"run_id":run_id,"source_id":cell["source_id"],"kind":cell["dimension_kind"],"side":cell["side"],"session":cell["session_bucket"],"n":cell["sample_count"],"gate":cell["n_gate_met"],"primary":PRIMARY_ESTIMATE_KIND,"posterior":json.dumps(cell["posterior"],sort_keys=True),"descriptive":json.dumps(cell["descriptive"],sort_keys=True),"status":STATISTICAL_STATUS})
            session.execute(text("UPDATE provider_fingerprint_runs SET completed_at=now(),engineering_status='ENGINEERING_PROVEN',statistical_status=:status WHERE id=:run_id"), {"status":STATISTICAL_STATUS,"run_id":run_id})
            session.commit()
            summary = {"run_id":str(run_id),"model_version":MODEL_VERSION,"engineering_status":"ENGINEERING_PROVEN","statistical_status":STATISTICAL_STATUS,"minimum_forward_n":MINIMUM_FORWARD_N,"provider_count":provider_count,"eligible_trade_count":len(observations),"cell_count":len(cells),"evidence_digest":digest,"primary_estimate_kind":PRIMARY_ESTIMATE_KIND,"research_only":True,"live_money_execution_allowed":False}
            print("PROVIDER_DAY12_FINGERPRINT=" + json.dumps(summary, sort_keys=True), flush=True)
            return summary
    finally:
        try:
            lock.execute(text("SELECT pg_advisory_unlock(hashtext('provider_day12_fingerprint_v1'))"))
        finally:
            lock.close()


if __name__ == "__main__":
    run()
