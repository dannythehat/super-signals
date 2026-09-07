"""Provider Intelligence Day 14 research promotion/demotion governance.

This module is deliberately broker-isolated. It consumes only the latest completed Day 13
conditional research run plus shadow-provider research metadata. It may advance the separate
research governance stage (shadow -> supervised -> paper_candidate -> tiny_live_candidate)
when a future, separately validated evidence source permits that, but it never mutates
`sources.status`, routes or sizes trades, calls MetaAPI, or grants live-money authority.

The final stage here is only `tiny_live_candidate`. Actual live activation is outside this
state machine and requires a separate explicit owner-gated mechanism.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import text

from app.db import get_engine, get_session_factory

MODEL_VERSION = "provider_day14_v1"
STAGES = ("shadow", "supervised", "paper_candidate", "tiny_live_candidate")
ACTIONS = ("HOLD", "PROMOTE", "DEMOTE", "RETEST")
STATISTICALLY_VALIDATED = "STATISTICALLY_VALIDATED"
OWNER_APPROVED_THRESHOLDS = "OWNER_APPROVED"
WAITING_FORWARD = "WAITING_FORWARD_EVIDENCE"
WAITING_THRESHOLD = "WAITING_THRESHOLD_APPROVAL"
LIVE_GATE = "REQUIRED_BEFORE_LIVE"
NO_LIVE_GATE = "NOT_ELIGIBLE"


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    proposed_action: str
    proposed_stage: str
    evidence_state: str
    human_gate_status: str
    eligible_for_human_review: bool
    reason: str


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _code_sha() -> str:
    for name in ("RENDER_GIT_COMMIT", "GITHUB_SHA", "SOURCE_COMMIT"):
        value = (os.getenv(name) or "").strip()
        if len(value) == 40:
            return value
    return "0" * 40


def _previous_stage(stage: str) -> str:
    if stage not in STAGES:
        raise ValueError("unknown_rollout_stage")
    index = STAGES.index(stage)
    return STAGES[max(0, index - 1)]


def _next_stage(stage: str) -> str:
    if stage not in STAGES:
        raise ValueError("unknown_rollout_stage")
    index = STAGES.index(stage)
    return STAGES[min(len(STAGES) - 1, index + 1)]


def decide_governance(
    *,
    current_stage: str,
    research_profile_state: str,
    source_statistical_status: str,
    source_threshold_approval_status: str,
    positive_candidate_count: int,
    negative_candidate_count: int,
    drifted: bool = False,
) -> GovernanceDecision:
    """Deterministic research-stage proposal policy with a hard live gate."""
    if current_stage not in STAGES:
        raise ValueError("unknown_rollout_stage")
    if positive_candidate_count < 0 or negative_candidate_count < 0:
        raise ValueError("candidate_counts_must_be_nonnegative")

    if research_profile_state == "duplicate_review":
        return GovernanceDecision(
            "RETEST", "shadow", "DUPLICATE_REVIEW", NO_LIVE_GATE, False,
            "duplicate evidence requires re-test before any promotion",
        )

    if drifted:
        if current_stage == "shadow":
            return GovernanceDecision(
                "RETEST", "shadow", "DRIFTED", NO_LIVE_GATE, False,
                "drift at shadow stage requires re-test",
            )
        return GovernanceDecision(
            "DEMOTE", _previous_stage(current_stage), "DRIFTED", NO_LIVE_GATE, False,
            "drift requires one-stage research demotion and re-test",
        )

    if source_statistical_status != STATISTICALLY_VALIDATED:
        return GovernanceDecision(
            "HOLD", current_stage, WAITING_FORWARD, NO_LIVE_GATE, False,
            "source statistical authority is not validated",
        )

    if source_threshold_approval_status != OWNER_APPROVED_THRESHOLDS:
        return GovernanceDecision(
            "HOLD", current_stage, WAITING_THRESHOLD, NO_LIVE_GATE, False,
            "statistical thresholds do not have explicit owner approval",
        )

    if positive_candidate_count and negative_candidate_count:
        if current_stage == "shadow":
            return GovernanceDecision(
                "RETEST", "shadow", "CONFLICTING_EVIDENCE", NO_LIVE_GATE, False,
                "positive and adverse validated conditions conflict",
            )
        return GovernanceDecision(
            "DEMOTE", _previous_stage(current_stage), "CONFLICTING_EVIDENCE", NO_LIVE_GATE, False,
            "conflicting validated conditions require one-stage demotion",
        )

    if negative_candidate_count:
        if current_stage == "shadow":
            return GovernanceDecision(
                "RETEST", "shadow", "VALIDATED_NEGATIVE", NO_LIVE_GATE, False,
                "validated adverse edge requires re-test at shadow stage",
            )
        return GovernanceDecision(
            "DEMOTE", _previous_stage(current_stage), "VALIDATED_NEGATIVE", NO_LIVE_GATE, False,
            "validated adverse edge requires one-stage research demotion",
        )

    if positive_candidate_count:
        if current_stage == "tiny_live_candidate":
            return GovernanceDecision(
                "HOLD", current_stage, "VALIDATED_POSITIVE", LIVE_GATE, True,
                "research state machine stops at tiny-live candidate; owner live gate is mandatory",
            )
        target = _next_stage(current_stage)
        human_gate = LIVE_GATE if target == "tiny_live_candidate" else NO_LIVE_GATE
        return GovernanceDecision(
            "PROMOTE", target, "VALIDATED_POSITIVE", human_gate,
            target == "tiny_live_candidate",
            "validated positive edge permits one research-stage promotion only",
        )

    return GovernanceDecision(
        "HOLD", current_stage, "NO_VALIDATED_EDGE", NO_LIVE_GATE, False,
        "no validated conditional edge supports a stage change",
    )


def simulated_governance_acceptance() -> dict[str, Any]:
    """Synthetic state-machine acceptance only; never provider evidence."""
    promoted = decide_governance(
        current_stage="supervised",
        research_profile_state="learning",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=2,
        negative_candidate_count=0,
    )
    tiny_candidate = decide_governance(
        current_stage="paper_candidate",
        research_profile_state="qualified",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=3,
        negative_candidate_count=0,
    )
    demoted = decide_governance(
        current_stage="paper_candidate",
        research_profile_state="qualified",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=0,
        negative_candidate_count=2,
    )
    drift = decide_governance(
        current_stage="supervised",
        research_profile_state="learning",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status=OWNER_APPROVED_THRESHOLDS,
        positive_candidate_count=0,
        negative_candidate_count=0,
        drifted=True,
    )
    unapproved = decide_governance(
        current_stage="shadow",
        research_profile_state="learning",
        source_statistical_status=STATISTICALLY_VALIDATED,
        source_threshold_approval_status="PROPOSED_UNAPPROVED",
        positive_candidate_count=10,
        negative_candidate_count=0,
    )
    passed = bool(
        promoted.proposed_action == "PROMOTE"
        and promoted.proposed_stage == "paper_candidate"
        and tiny_candidate.proposed_stage == "tiny_live_candidate"
        and tiny_candidate.human_gate_status == LIVE_GATE
        and tiny_candidate.eligible_for_human_review
        and demoted.proposed_action == "DEMOTE"
        and demoted.proposed_stage == "supervised"
        and drift.proposed_action == "DEMOTE"
        and unapproved.proposed_action == "HOLD"
    )
    return {
        "synthetic_only": True,
        "promotion": asdict(promoted),
        "tiny_live_candidate": asdict(tiny_candidate),
        "demotion": asdict(demoted),
        "drift": asdict(drift),
        "unapproved_thresholds": asdict(unapproved),
        "acceptance_passed": passed,
        "statistical_authority_granted": False,
        "live_money_authority_granted": False,
    }


def _latest_day13_run(session: Any) -> Mapping[str, Any]:
    row = session.execute(
        text(
            """
            SELECT id,code_sha,engineering_status,statistical_status,threshold_approval_status,
                   provider_count,eligible_oos_trade_count,tested_hypothesis_count,
                   bh_rejected_count,builder_gate_candidate_count,authoritative_discovery_count,
                   evidence_digest,completed_at,research_only,live_money_execution_allowed
            FROM provider_conditional_runs
            WHERE model_version='provider_day13_v1' AND completed_at IS NOT NULL
            ORDER BY completed_at DESC,id DESC LIMIT 1
            """
        )
    ).mappings().first()
    if row is None:
        raise RuntimeError("day13_completed_run_required")
    if not bool(row["research_only"]) or bool(row["live_money_execution_allowed"]):
        raise RuntimeError("day13_research_boundary_invalid")
    if int(row["authoritative_discovery_count"] or 0) != 0:
        raise RuntimeError("day13_authoritative_discovery_not_permitted")
    return row


def _shadow_providers(session: Any) -> list[Mapping[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT s.id AS source_id,COALESCE(prp.research_state,'learning') AS research_profile_state
            FROM sources s
            LEFT JOIN provider_research_profiles prp ON prp.source_id=s.id
            WHERE s.status='shadow'
            ORDER BY s.id
            """
        )
    ).mappings().all()
    return list(rows)


def _ensure_governance_states(session: Any, providers: list[Mapping[str, Any]]) -> None:
    if not providers:
        return
    session.execute(
        text(
            """
            INSERT INTO provider_governance_states(source_id,rollout_stage,research_only,live_money_execution_allowed)
            VALUES (:source_id,'shadow',true,false)
            ON CONFLICT (source_id) DO NOTHING
            """
        ),
        [{"source_id": row["source_id"]} for row in providers],
    )


def _candidate_counts(session: Any, run_id: UUID) -> dict[UUID, tuple[int, int]]:
    rows = session.execute(
        text(
            """
            SELECT source_id,
                   COUNT(*) FILTER (WHERE builder_gate_candidate AND shrunken_effect_r > 0) AS positive,
                   COUNT(*) FILTER (WHERE builder_gate_candidate AND shrunken_effect_r < 0) AS negative
            FROM provider_conditional_results
            WHERE run_id=:run_id
            GROUP BY source_id
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    return {
        UUID(str(row["source_id"])): (int(row["positive"] or 0), int(row["negative"] or 0))
        for row in rows
    }


def _governance_status(source_run: Mapping[str, Any]) -> str:
    if str(source_run["statistical_status"]) != STATISTICALLY_VALIDATED:
        return "WAITING-FOR-FORWARD-EVIDENCE"
    if str(source_run["threshold_approval_status"]) != OWNER_APPROVED_THRESHOLDS:
        return "WAITING-FOR-THRESHOLD-APPROVAL"
    return "RESEARCH-PROPOSALS-READY"


def run() -> dict[str, Any]:
    session_factory = get_session_factory()
    code_sha = _code_sha()
    lock = get_engine().connect()
    acquired = bool(lock.execute(text("SELECT pg_try_advisory_lock(hashtext('provider_day14_governance_v1'))")).scalar_one())
    if not acquired:
        lock.close()
        return {
            "engineering_status": "SKIPPED_LOCK_HELD",
            "research_only": True,
            "live_money_execution_allowed": False,
        }

    try:
        with session_factory() as session:
            source_run = _latest_day13_run(session)
            source_run_id = UUID(str(source_run["id"]))
            existing = session.execute(
                text(
                    """
                    SELECT id,engineering_status,governance_status,evidence_digest
                    FROM provider_governance_runs
                    WHERE source_conditional_run_id=:source_run_id
                      AND model_version=:model AND code_sha=:sha AND completed_at IS NOT NULL
                    LIMIT 1
                    """
                ),
                {"source_run_id": source_run_id, "model": MODEL_VERSION, "sha": code_sha},
            ).mappings().first()
            if existing:
                return {
                    "run_id": str(existing["id"]),
                    "engineering_status": existing["engineering_status"],
                    "governance_status": existing["governance_status"],
                    "evidence_digest": existing["evidence_digest"],
                    "skipped_existing_code_sha": True,
                    "research_only": True,
                    "live_money_execution_allowed": False,
                }

            providers = _shadow_providers(session)
            if int(source_run["provider_count"] or 0) != len(providers):
                raise RuntimeError("day13_shadow_provider_count_mismatch")
            _ensure_governance_states(session, providers)
            session.flush()

            stage_rows = session.execute(
                text(
                    """
                    SELECT g.source_id,g.rollout_stage,g.state_version
                    FROM provider_governance_states g
                    JOIN sources s ON s.id=g.source_id
                    WHERE s.status='shadow'
                    ORDER BY g.source_id
                    """
                )
            ).mappings().all()
            stage_by_source = {
                UUID(str(row["source_id"])): (str(row["rollout_stage"]), int(row["state_version"]))
                for row in stage_rows
            }
            candidates = _candidate_counts(session, source_run_id)
            governance_status = _governance_status(source_run)
            simulation = simulated_governance_acceptance()
            engineering_status = "ENGINEERING_PROVEN" if simulation["acceptance_passed"] else "FAILED_SIMULATION_ACCEPTANCE"

            decisions: list[dict[str, Any]] = []
            for provider in providers:
                source_id = UUID(str(provider["source_id"]))
                current_stage, state_version = stage_by_source[source_id]
                positive, negative = candidates.get(source_id, (0, 0))
                decision = decide_governance(
                    current_stage=current_stage,
                    research_profile_state=str(provider["research_profile_state"]),
                    source_statistical_status=str(source_run["statistical_status"]),
                    source_threshold_approval_status=str(source_run["threshold_approval_status"]),
                    positive_candidate_count=positive,
                    negative_candidate_count=negative,
                )
                transition = decision.proposed_stage != current_stage and decision.proposed_action in {"PROMOTE", "DEMOTE", "RETEST"}
                # Promotion/demotion require separately validated and owner-approved statistics;
                # duplicate/drift re-tests may only move the research stage back toward shadow.
                # No transition here can change sources.status or create broker authority.
                if transition:
                    session.execute(
                        text(
                            """
                            UPDATE provider_governance_states
                            SET rollout_stage=:stage,state_version=state_version+1,
                                last_transition_at=now(),updated_at=now()
                            WHERE source_id=:source_id AND state_version=:state_version
                            """
                        ),
                        {"stage": decision.proposed_stage, "source_id": source_id, "state_version": state_version},
                    )
                decisions.append(
                    {
                        "source_id": source_id,
                        "research_profile_state": str(provider["research_profile_state"]),
                        "current_rollout_stage": current_stage,
                        "proposed_rollout_stage": decision.proposed_stage,
                        "proposed_action": decision.proposed_action,
                        "evidence_state": decision.evidence_state,
                        "positive_candidate_count": positive,
                        "negative_candidate_count": negative,
                        "human_gate_status": decision.human_gate_status,
                        "eligible_for_human_review": decision.eligible_for_human_review,
                        "research_stage_transitioned": transition,
                        "reason_json": {"reason": decision.reason, "source_day13_run_id": str(source_run_id)},
                    }
                )

            evidence_payload = {
                "source_day13_run_id": str(source_run_id),
                "source_day13_digest": source_run["evidence_digest"],
                "source_statistical_status": source_run["statistical_status"],
                "source_threshold_approval_status": source_run["threshold_approval_status"],
                "providers": [
                    {
                        "source_id": str(row["source_id"]),
                        "research_profile_state": row["research_profile_state"],
                        "current_rollout_stage": row["current_rollout_stage"],
                        "proposed_rollout_stage": row["proposed_rollout_stage"],
                        "proposed_action": row["proposed_action"],
                        "evidence_state": row["evidence_state"],
                        "positive_candidate_count": row["positive_candidate_count"],
                        "negative_candidate_count": row["negative_candidate_count"],
                    }
                    for row in decisions
                ],
            }
            evidence_digest = _digest(evidence_payload)
            counts = {action: sum(row["proposed_action"] == action for row in decisions) for action in ACTIONS}
            transition_count = sum(bool(row["research_stage_transitioned"]) for row in decisions)
            human_review_count = sum(bool(row["eligible_for_human_review"]) for row in decisions)

            run_id = session.execute(
                text(
                    """
                    INSERT INTO provider_governance_runs(
                        source_conditional_run_id,model_version,code_sha,source_statistical_status,
                        source_threshold_approval_status,engineering_status,governance_status,
                        provider_count,hold_count,promotion_proposal_count,demotion_proposal_count,
                        retest_proposal_count,research_stage_transition_count,human_review_count,
                        authoritative_transition_count,simulation_json,evidence_digest,research_only,
                        live_money_execution_allowed
                    ) VALUES (
                        :source_run,:model,:sha,:stat_status,:threshold_status,:engineering,:governance,
                        :providers,:holds,:promotions,:demotions,:retests,:transitions,:human_reviews,
                        0,CAST(:simulation AS jsonb),:digest,true,false
                    ) RETURNING id
                    """
                ),
                {
                    "source_run": source_run_id,
                    "model": MODEL_VERSION,
                    "sha": code_sha,
                    "stat_status": str(source_run["statistical_status"]),
                    "threshold_status": str(source_run["threshold_approval_status"]),
                    "engineering": engineering_status,
                    "governance": governance_status,
                    "providers": len(decisions),
                    "holds": counts["HOLD"],
                    "promotions": counts["PROMOTE"],
                    "demotions": counts["DEMOTE"],
                    "retests": counts["RETEST"],
                    "transitions": transition_count,
                    "human_reviews": human_review_count,
                    "simulation": json.dumps(simulation, sort_keys=True),
                    "digest": evidence_digest,
                },
            ).scalar_one()

            if decisions:
                session.execute(
                    text(
                        """
                        INSERT INTO provider_governance_results(
                            run_id,source_id,research_profile_state,current_rollout_stage,
                            proposed_rollout_stage,proposed_action,evidence_state,
                            positive_candidate_count,negative_candidate_count,human_gate_status,
                            eligible_for_human_review,research_stage_transitioned,
                            authoritative_transition,reason_json,research_only,live_money_execution_allowed
                        ) VALUES (
                            :run_id,:source_id,:research_profile_state,:current_rollout_stage,
                            :proposed_rollout_stage,:proposed_action,:evidence_state,
                            :positive_candidate_count,:negative_candidate_count,:human_gate_status,
                            :eligible_for_human_review,:research_stage_transitioned,false,
                            CAST(:reason_json AS jsonb),true,false
                        )
                        """
                    ),
                    [
                        {
                            **{key: value for key, value in row.items() if key != "reason_json"},
                            "run_id": run_id,
                            "reason_json": json.dumps(row["reason_json"], sort_keys=True),
                        }
                        for row in decisions
                    ],
                )

            session.execute(
                text("UPDATE provider_governance_runs SET completed_at=now() WHERE id=:run_id"),
                {"run_id": run_id},
            )
            session.commit()

            summary = {
                "run_id": str(run_id),
                "model_version": MODEL_VERSION,
                "code_sha": code_sha,
                "source_day13_run_id": str(source_run_id),
                "source_statistical_status": str(source_run["statistical_status"]),
                "source_threshold_approval_status": str(source_run["threshold_approval_status"]),
                "engineering_status": engineering_status,
                "governance_status": governance_status,
                "provider_count": len(decisions),
                "hold_count": counts["HOLD"],
                "promotion_proposal_count": counts["PROMOTE"],
                "demotion_proposal_count": counts["DEMOTE"],
                "retest_proposal_count": counts["RETEST"],
                "research_stage_transition_count": transition_count,
                "human_review_count": human_review_count,
                "authoritative_transition_count": 0,
                "simulation_acceptance_passed": bool(simulation["acceptance_passed"]),
                "evidence_digest": evidence_digest,
                "research_only": True,
                "live_money_execution_allowed": False,
            }
            print("PROVIDER_DAY14_GOVERNANCE=" + json.dumps(summary, sort_keys=True), flush=True)
            return summary
    finally:
        try:
            lock.execute(text("SELECT pg_advisory_unlock(hashtext('provider_day14_governance_v1'))"))
        finally:
            lock.close()


if __name__ == "__main__":
    run()
