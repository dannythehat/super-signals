"""Provider Intelligence governance runtime with provider-specific OOS boundaries.

The original governance runner assumed every provider was preregistered at the same
instant. That became false as soon as TIG was added later, and production consequently
raised ``day13_preregistration_boundary_not_frozen`` on every governance pass.

This runtime preserves the existing research-only governance constitution but resolves
one immutable preregistration boundary *per provider*. No live routing/provider status,
broker order, sizing, or live-money authority is touched here.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app import provider_day13_conditional as day13
from app import provider_day13_runtime as learning_runtime
from app import provider_day14_governance as governance
from app.db import get_engine, get_session_factory

_MODEL_VERSION = "provider_day14_v2_provider_boundaries"
_LOCK_NAME = "provider_day14_governance_v2"


def _source_boundaries(session: Any) -> dict[UUID, datetime]:
    rows = session.execute(
        text(
            """
            SELECT source_id,
                   MIN(preregistered_at) AS first_boundary,
                   MAX(preregistered_at) AS last_boundary,
                   COUNT(DISTINCT preregistered_at) AS boundary_count
            FROM provider_conditional_hypotheses
            WHERE registry_version=:registry
            GROUP BY source_id
            ORDER BY source_id
            """
        ),
        {"registry": day13.REGISTRY_VERSION},
    ).mappings().all()
    result: dict[UUID, datetime] = {}
    for row in rows:
        if int(row["boundary_count"] or 0) != 1:
            raise RuntimeError("provider_preregistration_boundary_not_frozen")
        boundary = row["first_boundary"]
        if not isinstance(boundary, datetime):
            raise RuntimeError("provider_preregistration_boundary_missing")
        result[UUID(str(row["source_id"]))] = boundary.astimezone(UTC)
    if not result:
        raise RuntimeError("provider_preregistration_boundary_missing")
    return result


def _governance_code_sha() -> str:
    for name in ("RENDER_GIT_COMMIT", "GITHUB_SHA", "SOURCE_COMMIT"):
        value = (os.getenv(name) or "").strip()
        if len(value) == 40:
            return value
    return "0" * 40


def run() -> dict[str, Any]:
    session_factory = get_session_factory()
    code_sha = _governance_code_sha()
    cutoff = datetime.now(UTC)
    lock = get_engine().connect()
    acquired = bool(
        lock.execute(text(f"SELECT pg_try_advisory_lock(hashtext('{_LOCK_NAME}'))")).scalar_one()
    )
    if not acquired:
        lock.close()
        return {
            "engineering_status": "SKIPPED_LOCK_HELD",
            "research_only": True,
            "live_money_execution_allowed": False,
        }

    try:
        with session_factory() as session:
            policy = governance._load_policy(session)
            source_run = governance._latest_day13_run(session)
            source_run_id = UUID(str(source_run["id"]))
            profiles = governance._shadow_profiles(session)
            boundaries = _source_boundaries(session)

            hypotheses = day13._load_registry(session)
            observations = learning_runtime._load_observations(session, cutoff=cutoff)
            conditional_results, _eligible_trade_ids = day13.build_conditional_results(
                hypotheses,
                observations,
            )

            evidence_by_source: dict[UUID, governance.ProviderEvidence] = {}
            for profile in profiles:
                source_id = UUID(str(profile["source_id"]))
                boundary = boundaries.get(source_id)
                if boundary is None:
                    raise RuntimeError("provider_preregistration_boundary_missing")
                evidence_by_source[source_id] = governance._provider_evidence(
                    source_id=source_id,
                    observations=observations,
                    conditional_results=conditional_results,
                    frozen_boundary=boundary,
                    policy=policy,
                )

            boundary_payload = {
                str(source_id): boundary.isoformat()
                for source_id, boundary in sorted(boundaries.items(), key=lambda item: str(item[0]))
            }
            global_evidence_payload = {
                "policy_version": governance.POLICY_VERSION,
                "policy_approval_status": str(policy["approval_status"]),
                "provider_boundaries": boundary_payload,
                "providers": [
                    {
                        "source_id": str(source_id),
                        "research_state": str(profile["research_state"]),
                        "provider_evidence_digest": evidence_by_source[source_id].evidence_digest,
                    }
                    for profile in profiles
                    for source_id in [UUID(str(profile["source_id"]))]
                ],
            }
            evidence_digest = governance._digest(global_evidence_payload)
            existing = session.execute(
                text(
                    """
                    SELECT id,engineering_status,governance_status,evidence_digest
                    FROM provider_governance_runs
                    WHERE policy_version=:policy AND model_version=:model
                      AND evidence_digest=:digest AND completed_at IS NOT NULL
                    LIMIT 1
                    """
                ),
                {
                    "policy": governance.POLICY_VERSION,
                    "model": _MODEL_VERSION,
                    "digest": evidence_digest,
                },
            ).mappings().first()
            if existing:
                return {
                    "run_id": str(existing["id"]),
                    "engineering_status": existing["engineering_status"],
                    "governance_status": existing["governance_status"],
                    "evidence_digest": existing["evidence_digest"],
                    "skipped_unchanged_evidence": True,
                    "provider_boundary_count": len(set(boundary_payload.values())),
                    "research_only": True,
                    "live_money_execution_allowed": False,
                }

            simulation = governance.simulated_governance_acceptance()
            engineering_status = (
                "ENGINEERING_PROVEN"
                if simulation["acceptance_passed"]
                else "FAILED_SIMULATION_ACCEPTANCE"
            )
            decisions: list[dict[str, Any]] = []
            for profile in profiles:
                source_id = UUID(str(profile["source_id"]))
                current_state = str(profile["research_state"])
                evidence = evidence_by_source[source_id]
                boundary = boundaries[source_id]
                prior = governance._prior_result(session, source_id)
                sustained_decay = bool(
                    evidence.evidence_state == "NEGATIVE_CONFIDENT"
                    and prior is not None
                    and str(prior["evidence_state"]) == "NEGATIVE_CONFIDENT"
                    and evidence.oos_trade_count > int(prior["oos_trade_count"] or 0)
                )
                last_transition_digest = None
                if prior is not None and bool(prior["research_state_transitioned"]):
                    last_transition_digest = str(prior["provider_evidence_digest"])
                fresh = last_transition_digest != evidence.evidence_digest
                duplicate = (
                    profile["duplicate_of_source_id"] is not None
                    or current_state == "duplicate_review"
                )
                decision = governance.decide_governance(
                    current_research_state=current_state,
                    policy_approval_status=str(policy["approval_status"]),
                    evidence_state=evidence.evidence_state,
                    sustained_decay=sustained_decay,
                    duplicate_review=duplicate,
                    fresh_evidence_since_transition=fresh,
                )
                transition = (
                    decision.proposed_research_state != current_state
                    and decision.proposed_action in {"PROMOTE", "DEMOTE"}
                )
                if transition:
                    changed = session.execute(
                        text(
                            """
                            UPDATE provider_research_profiles
                            SET research_state=:new_state,updated_at=now()
                            WHERE source_id=:source_id AND research_state=:current_state
                            """
                        ),
                        {
                            "new_state": decision.proposed_research_state,
                            "source_id": source_id,
                            "current_state": current_state,
                        },
                    )
                    if changed.rowcount != 1:
                        raise RuntimeError("provider_governance_state_cas_failed")
                decisions.append(
                    {
                        "source_id": source_id,
                        "current_research_state": current_state,
                        "proposed_research_state": decision.proposed_research_state,
                        "proposed_action": decision.proposed_action,
                        "evidence": evidence,
                        "provider_boundary": boundary,
                        "sustained_decay": sustained_decay,
                        "duplicate_review": duplicate,
                        "paper_qualified": decision.paper_qualified,
                        "research_state_transitioned": transition,
                        "reason": decision.reason,
                    }
                )

            governance_status = governance._governance_status(
                policy,
                list(evidence_by_source.values()),
            )
            counts = {
                action: sum(row["proposed_action"] == action for row in decisions)
                for action in governance.ACTIONS
            }
            transition_count = sum(bool(row["research_state_transitioned"]) for row in decisions)
            paper_qualified_count = sum(bool(row["paper_qualified"]) for row in decisions)

            run_id = session.execute(
                text(
                    """
                    INSERT INTO provider_governance_runs(
                        policy_version,source_conditional_run_id,model_version,code_sha,
                        policy_approval_status,engineering_status,governance_status,provider_count,
                        hold_count,promotion_count,demotion_count,retest_count,
                        research_state_transition_count,paper_qualified_count,
                        authoritative_live_transition_count,simulation_json,evidence_digest,
                        research_only,live_money_execution_allowed
                    ) VALUES (
                        :policy,:source_run,:model,:sha,:approval,:engineering,:governance,:providers,
                        :holds,:promotions,:demotions,:retests,:transitions,:paper_qualified,
                        0,CAST(:simulation AS jsonb),:digest,true,false
                    ) RETURNING id
                    """
                ),
                {
                    "policy": governance.POLICY_VERSION,
                    "source_run": source_run_id,
                    "model": _MODEL_VERSION,
                    "sha": code_sha,
                    "approval": str(policy["approval_status"]),
                    "engineering": engineering_status,
                    "governance": governance_status,
                    "providers": len(decisions),
                    "holds": counts["HOLD"],
                    "promotions": counts["PROMOTE"],
                    "demotions": counts["DEMOTE"],
                    "retests": counts["RETEST"],
                    "transitions": transition_count,
                    "paper_qualified": paper_qualified_count,
                    "simulation": json.dumps(
                        {
                            **simulation,
                            "provider_boundaries": boundary_payload,
                        },
                        sort_keys=True,
                    ),
                    "digest": evidence_digest,
                },
            ).scalar_one()

            if decisions:
                session.execute(
                    text(
                        """
                        INSERT INTO provider_governance_results(
                            run_id,source_id,current_research_state,proposed_research_state,
                            proposed_action,evidence_state,oos_trade_count,mean_quality_r,lower_95_r,
                            upper_95_r,tested_fingerprint_cell_count,positive_candidate_count,
                            negative_candidate_count,provider_evidence_digest,sustained_decay,
                            duplicate_review,paper_qualified,human_live_gate_required,
                            research_state_transitioned,authoritative_live_transition,reason_json,
                            research_only,live_money_execution_allowed
                        ) VALUES (
                            :run_id,:source_id,:current_state,:proposed_state,:action,:evidence_state,
                            :oos_n,:mean_r,:lower_r,:upper_r,:tested,:positive,:negative,:provider_digest,
                            :sustained,:duplicate,:paper,true,:transitioned,false,CAST(:reason AS jsonb),true,false
                        )
                        """
                    ),
                    [
                        {
                            "run_id": run_id,
                            "source_id": row["source_id"],
                            "current_state": row["current_research_state"],
                            "proposed_state": row["proposed_research_state"],
                            "action": row["proposed_action"],
                            "evidence_state": row["evidence"].evidence_state,
                            "oos_n": row["evidence"].oos_trade_count,
                            "mean_r": row["evidence"].mean_quality_r,
                            "lower_r": row["evidence"].lower_95_r,
                            "upper_r": row["evidence"].upper_95_r,
                            "tested": row["evidence"].tested_fingerprint_cell_count,
                            "positive": row["evidence"].positive_candidate_count,
                            "negative": row["evidence"].negative_candidate_count,
                            "provider_digest": row["evidence"].evidence_digest,
                            "sustained": row["sustained_decay"],
                            "duplicate": row["duplicate_review"],
                            "paper": row["paper_qualified"],
                            "transitioned": row["research_state_transitioned"],
                            "reason": json.dumps(
                                {
                                    "reason": row["reason"],
                                    "policy_approval_status": str(policy["approval_status"]),
                                    "provider_frozen_oos_boundary": row[
                                        "provider_boundary"
                                    ].isoformat(),
                                },
                                sort_keys=True,
                            ),
                        }
                        for row in decisions
                    ],
                )

            session.execute(
                text("UPDATE provider_governance_runs SET completed_at=now() WHERE id=:id"),
                {"id": run_id},
            )
            session.commit()

            boundary_values = sorted(set(boundary_payload.values()))
            summary = {
                "run_id": str(run_id),
                "model_version": _MODEL_VERSION,
                "policy_version": governance.POLICY_VERSION,
                "policy_approval_status": str(policy["approval_status"]),
                "code_sha": code_sha,
                "source_learning_run_id": str(source_run_id),
                "engineering_status": engineering_status,
                "governance_status": governance_status,
                "provider_count": len(decisions),
                "provider_boundary_count": len(boundary_values),
                "first_provider_boundary": boundary_values[0] if boundary_values else None,
                "last_provider_boundary": boundary_values[-1] if boundary_values else None,
                "hold_count": counts["HOLD"],
                "promotion_count": counts["PROMOTE"],
                "demotion_count": counts["DEMOTE"],
                "retest_count": counts["RETEST"],
                "research_state_transition_count": transition_count,
                "paper_qualified_count": paper_qualified_count,
                "authoritative_live_transition_count": 0,
                "simulation_acceptance_passed": bool(simulation["acceptance_passed"]),
                "evidence_digest": evidence_digest,
                "research_only": True,
                "live_money_execution_allowed": False,
            }
            print("PROVIDER_GOVERNANCE_V2=" + json.dumps(summary, sort_keys=True), flush=True)
            return summary
    finally:
        try:
            lock.execute(text(f"SELECT pg_advisory_unlock(hashtext('{_LOCK_NAME}'))"))
        finally:
            lock.close()


def run_forever() -> None:
    interval = max(
        3600,
        int(os.getenv("SUPER_SIGNALS_PROVIDER_GOVERNANCE_SECONDS", "21600") or "21600"),
    )
    while True:
        try:
            run()
        except Exception as exc:
            print("PROVIDER_GOVERNANCE_V2_ERROR=" + type(exc).__name__, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
