"""Continuous production runner for Provider Intelligence conditional learning.

The statistical constitution remains in :mod:`provider_day13_conditional`. This runner
binds the canonical production context schema and appends a new research snapshot only
when the usable point-in-time/out-of-sample evidence digest changes. Retrospective
provider-score backfills are never read and this module has no broker/live-money path.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app import provider_day13_conditional as day13
from app.db import get_engine, get_session_factory

_DEFAULT_REFRESH_SECONDS = 900
_MIN_REFRESH_SECONDS = 300
_LOCK_NAME = "provider_day13_conditional_v1"


def _load_observations(session: Any, *, cutoff: datetime) -> list[day13.ConditionalObservation]:
    rows = session.execute(
        text(
            """
            SELECT t.id AS trade_id,t.source_id,t.signal_id,t.side,t.session_bucket,
                   t.opened_at,t.closed_at,t.quality_r_multiple,
                   a.signal_posted_at,a.aidy_context_as_of_utc,a.provider_profile_effective_at,a.regime_json
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
              AND a.aidy_context_as_of_utc<=a.signal_posted_at
              AND a.provider_profile_effective_at<=a.signal_posted_at
              AND a.regime_json->>'regime_definition_version'=:regime_version
            ORDER BY t.source_id,a.signal_posted_at,t.id
            """
        ),
        {"cutoff": cutoff, "regime_version": day13.AIDY_REGIME_DEFINITION_VERSION},
    ).mappings().all()

    observations: list[day13.ConditionalObservation] = []
    for row in rows:
        duration_seconds = (row["closed_at"] - row["opened_at"]).total_seconds()
        if duration_seconds < 0:
            continue
        labels = day13._known_regime_labels(row["regime_json"])
        if not labels:
            continue
        observations.append(
            day13.ConditionalObservation(
                trade_id=UUID(str(row["trade_id"])),
                source_id=UUID(str(row["source_id"])),
                signal_id=UUID(str(row["signal_id"])),
                side=str(row["side"]),
                session_bucket=str(row["session_bucket"]),
                signal_posted_at=row["signal_posted_at"].astimezone(UTC),
                duration_bucket=day13.duration_bucket(duration_seconds),
                realized_r=float(row["quality_r_multiple"]),
                regime_labels=labels,
            )
        )
    return observations


def _existing_evidence_run(
    session: Any,
    *,
    code_sha: str,
    evidence_digest: str,
) -> Any | None:
    return session.execute(
        text(
            """
            SELECT id,engineering_status,statistical_status,evidence_digest,
                   eligible_oos_trade_count,result_count,completed_at
            FROM provider_conditional_runs
            WHERE model_version=:model AND code_sha=:sha
              AND evidence_digest=:digest AND completed_at IS NOT NULL
            ORDER BY completed_at DESC,id DESC
            LIMIT 1
            """
        ),
        {
            "model": day13.MODEL_VERSION,
            "sha": code_sha,
            "digest": evidence_digest,
        },
    ).mappings().first()


def _persist_results(session: Any, *, run_id: UUID, results: list[dict[str, Any]]) -> None:
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
                "approval": day13.THRESHOLD_APPROVAL_STATUS,
                "status": day13.STATISTICAL_STATUS,
            },
        )


def run() -> dict[str, Any]:
    """Append one conditional snapshot only when forward PIT/OOS evidence changed."""
    session_factory = get_session_factory()
    code_sha = day13._code_sha()
    cutoff = datetime.now(UTC)
    lock = get_engine().connect()
    acquired = bool(
        lock.execute(text(f"SELECT pg_try_advisory_lock(hashtext('{_LOCK_NAME}'))")).scalar_one()
    )
    if not acquired:
        lock.close()
        return {
            "engineering_status": "SKIPPED_LOCK_HELD",
            "statistical_status": day13.STATISTICAL_STATUS,
            "threshold_approval_status": day13.THRESHOLD_APPROVAL_STATUS,
            "research_only": True,
            "live_money_execution_allowed": False,
        }

    try:
        with session_factory() as session:
            provider_count = day13._ensure_preregistry(session, preregistered_at=cutoff)
            session.commit()
            hypotheses = day13._load_registry(session)
            observations = _load_observations(session, cutoff=cutoff)
            results, eligible_trade_ids = day13.build_conditional_results(hypotheses, observations)
            digest = day13._evidence_digest(hypotheses, observations, eligible_trade_ids)

            existing = _existing_evidence_run(
                session,
                code_sha=code_sha,
                evidence_digest=digest,
            )
            if existing:
                return {
                    "run_id": str(existing["id"]),
                    "engineering_status": existing["engineering_status"],
                    "statistical_status": existing["statistical_status"],
                    "threshold_approval_status": day13.THRESHOLD_APPROVAL_STATUS,
                    "eligible_oos_trade_count": int(existing["eligible_oos_trade_count"]),
                    "result_count": int(existing["result_count"]),
                    "evidence_digest": existing["evidence_digest"],
                    "skipped_unchanged_evidence": True,
                    "continuous_learning_runtime": True,
                    "research_only": True,
                    "live_money_execution_allowed": False,
                }

            simulation = day13.simulated_governance_acceptance()
            engineering_status = (
                "ENGINEERING_PROVEN"
                if simulation["acceptance_passed"]
                else "FAILED_SIMULATION_ACCEPTANCE"
            )
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
                    "model": day13.MODEL_VERSION,
                    "registry": day13.REGISTRY_VERSION,
                    "sha": code_sha,
                    "cutoff": cutoff,
                    "min_n": day13.MINIMUM_OOS_N,
                    "fdr_q": day13.PROPOSED_FDR_Q,
                    "min_effect": day13.PROPOSED_MIN_EFFECT_R,
                    "approval": day13.THRESHOLD_APPROVAL_STATUS,
                    "family": day13.FDR_FAMILY,
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

            _persist_results(session, run_id=UUID(str(run_id)), results=results)
            session.execute(
                text(
                    "UPDATE provider_conditional_runs "
                    "SET completed_at=now(),engineering_status=:engineering_status,"
                    "statistical_status=:statistical_status WHERE id=:run_id"
                ),
                {
                    "engineering_status": engineering_status,
                    "statistical_status": day13.STATISTICAL_STATUS,
                    "run_id": run_id,
                },
            )
            session.commit()

            summary = {
                "run_id": str(run_id),
                "model_version": day13.MODEL_VERSION,
                "registry_version": day13.REGISTRY_VERSION,
                "code_sha": code_sha,
                "evidence_cutoff": cutoff.isoformat(),
                "engineering_status": engineering_status,
                "statistical_status": day13.STATISTICAL_STATUS,
                "threshold_approval_status": day13.THRESHOLD_APPROVAL_STATUS,
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
                "primary_estimate_kind": day13.PRIMARY_ESTIMATE_KIND,
                "continuous_learning_runtime": True,
                "research_only": True,
                "live_money_execution_allowed": False,
            }
            print("PROVIDER_LEARNING=" + json.dumps(summary, sort_keys=True), flush=True)
            return summary
    finally:
        try:
            lock.execute(text(f"SELECT pg_advisory_unlock(hashtext('{_LOCK_NAME}'))"))
        finally:
            lock.close()


def run_forever() -> None:
    # Recover the old v1 terminal-miss backlog before the first evidence run so newly
    # valid D1-only degraded context can immediately participate in forward research.
    try:
        from app.provider_context_v2_recovery import run as recover_context_v2

        recover_context_v2()
    except Exception as exc:
        print("PROVIDER_CONTEXT_V2_RECOVERY_ERROR=" + type(exc).__name__, flush=True)

    interval = max(
        _MIN_REFRESH_SECONDS,
        int(
            os.getenv(
                "SUPER_SIGNALS_PROVIDER_LEARNING_SECONDS",
                str(_DEFAULT_REFRESH_SECONDS),
            )
            or _DEFAULT_REFRESH_SECONDS
        ),
    )
    while True:
        try:
            run()
        except Exception as exc:
            print("PROVIDER_LEARNING_ERROR=" + type(exc).__name__, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    if "--forever" in sys.argv:
        run_forever()
    else:
        run()
