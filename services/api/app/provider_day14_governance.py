"""Provider Intelligence Day 14 evidence-backed provider governance.

Day 14 uses the existing provider_research_profiles state machine:
learning -> shadow -> qualified. Qualified means paper-research eligible only. A separate
human gate is mandatory before any real-money exposure, and this module has no broker,
routing, sizing or sources.status write path.

Promotion thresholds are stored as a builder recommendation and remain fail-closed until
an explicit owner approval is recorded. The evaluation is point-in-time and out-of-sample
relative to Day 13's immutable preregistration boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import fmean, variance
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import text

from app import provider_day13_conditional as day13
from app import provider_day13_runtime as day13_runtime
from app.db import get_engine, get_session_factory

MODEL_VERSION = "provider_day14_v1"
POLICY_VERSION = "provider_day14_policy_v1"
OWNER_APPROVED = "OWNER_APPROVED"
ACTIONS = ("HOLD", "PROMOTE", "DEMOTE", "RETEST")
RESEARCH_STATES = ("learning", "shadow", "duplicate_review", "qualified", "rejected")
_Z95 = 1.959963984540054


@dataclass(frozen=True, slots=True)
class ProviderEvidence:
    oos_trade_count: int
    mean_quality_r: float | None
    lower_95_r: float | None
    upper_95_r: float | None
    tested_fingerprint_cell_count: int
    positive_candidate_count: int
    negative_candidate_count: int
    evidence_state: str
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    proposed_action: str
    proposed_research_state: str
    paper_qualified: bool
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


def classify_performance(
    values: list[float],
    *,
    minimum_oos_n: int,
    tested_fingerprint_cells: int,
    minimum_tested_fingerprint_cells: int,
) -> tuple[str, float | None, float | None, float | None]:
    """Classify fair OOS R performance without turning small-N noise into a rule."""
    if len(values) < minimum_oos_n:
        mean = fmean(values) if values else None
        return "INSUFFICIENT_OOS", mean, None, None
    if tested_fingerprint_cells < minimum_tested_fingerprint_cells:
        mean = fmean(values) if values else None
        return "FINGERPRINT_NOT_READY", mean, None, None
    mean = fmean(values)
    sample_var = variance(values) if len(values) > 1 else 0.0
    standard_error = math.sqrt(max(sample_var, 0.0) / len(values))
    lower = mean - _Z95 * standard_error
    upper = mean + _Z95 * standard_error
    if lower > 0:
        state = "POSITIVE_CONFIDENT"
    elif upper < 0:
        state = "NEGATIVE_CONFIDENT"
    else:
        state = "UNCERTAIN"
    return state, mean, lower, upper


def decide_governance(
    *,
    current_research_state: str,
    policy_approval_status: str,
    evidence_state: str,
    sustained_decay: bool,
    duplicate_review: bool,
    fresh_evidence_since_transition: bool,
) -> GovernanceDecision:
    """Deterministic promotion/demotion/re-test policy for research state only."""
    if current_research_state not in RESEARCH_STATES:
        raise ValueError("unknown_provider_research_state")

    if duplicate_review or current_research_state == "duplicate_review":
        return GovernanceDecision(
            "RETEST", current_research_state, False,
            "duplicate evidence requires review before governance can advance",
        )

    if policy_approval_status != OWNER_APPROVED:
        return GovernanceDecision(
            "HOLD", current_research_state, current_research_state == "qualified",
            "promotion/demotion thresholds await explicit owner approval",
        )

    if evidence_state in {"INSUFFICIENT_OOS", "FINGERPRINT_NOT_READY", "UNCERTAIN"}:
        return GovernanceDecision(
            "HOLD", current_research_state, current_research_state == "qualified",
            "forward evidence does not clear the approved governance gate",
        )

    if not fresh_evidence_since_transition:
        return GovernanceDecision(
            "HOLD", current_research_state, current_research_state == "qualified",
            "same evidence snapshot cannot advance a provider through multiple states",
        )

    if evidence_state == "POSITIVE_CONFIDENT":
        if current_research_state == "learning":
            return GovernanceDecision("PROMOTE", "shadow", False, "positive OOS R plus tested fingerprint")
        if current_research_state == "shadow":
            return GovernanceDecision(
                "PROMOTE", "qualified", True,
                "positive OOS R plus tested fingerprint qualifies paper research only",
            )
        if current_research_state == "qualified":
            return GovernanceDecision(
                "HOLD", "qualified", True,
                "qualified is terminal for automatic governance; human live gate remains mandatory",
            )
        return GovernanceDecision(
            "RETEST", current_research_state, False,
            "rejected providers require an explicit re-test cycle rather than automatic resurrection",
        )

    if evidence_state == "NEGATIVE_CONFIDENT":
        if not sustained_decay:
            return GovernanceDecision(
                "RETEST", current_research_state, current_research_state == "qualified",
                "one adverse OOS snapshot triggers re-test, not automatic demotion",
            )
        if current_research_state == "qualified":
            return GovernanceDecision("DEMOTE", "shadow", False, "sustained statistically adverse OOS decay")
        if current_research_state == "shadow":
            return GovernanceDecision("DEMOTE", "learning", False, "sustained statistically adverse OOS decay")
        if current_research_state == "learning":
            return GovernanceDecision("DEMOTE", "rejected", False, "sustained statistically adverse OOS decay")
        return GovernanceDecision("HOLD", "rejected", False, "provider already rejected")

    raise ValueError("unknown_governance_evidence_state")


def simulated_governance_acceptance() -> dict[str, Any]:
    """Synthetic state-machine proof only; it is never provider evidence."""
    promotion_1 = decide_governance(
        current_research_state="learning",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    promotion_2 = decide_governance(
        current_research_state="shadow",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    first_bad = decide_governance(
        current_research_state="qualified",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="NEGATIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    sustained_bad = decide_governance(
        current_research_state="qualified",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="NEGATIVE_CONFIDENT",
        sustained_decay=True,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    unapproved = decide_governance(
        current_research_state="learning",
        policy_approval_status="PROPOSED_UNAPPROVED",
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=True,
    )
    replayed = decide_governance(
        current_research_state="shadow",
        policy_approval_status=OWNER_APPROVED,
        evidence_state="POSITIVE_CONFIDENT",
        sustained_decay=False,
        duplicate_review=False,
        fresh_evidence_since_transition=False,
    )
    passed = bool(
        promotion_1.proposed_research_state == "shadow"
        and promotion_2.proposed_research_state == "qualified"
        and promotion_2.paper_qualified
        and first_bad.proposed_action == "RETEST"
        and sustained_bad.proposed_research_state == "shadow"
        and unapproved.proposed_action == "HOLD"
        and replayed.proposed_action == "HOLD"
    )
    return {
        "synthetic_only": True,
        "learning_to_shadow": asdict(promotion_1),
        "shadow_to_qualified": asdict(promotion_2),
        "first_adverse_snapshot": asdict(first_bad),
        "sustained_decay": asdict(sustained_bad),
        "unapproved_policy": asdict(unapproved),
        "same_evidence_replay": asdict(replayed),
        "acceptance_passed": passed,
        "live_money_authority_granted": False,
    }


def _load_policy(session: Any) -> Mapping[str, Any]:
    row = session.execute(
        text(
            """
            SELECT policy_version,minimum_oos_n,minimum_tested_fingerprint_cells,
                   confidence_level,oos_window_mode,approval_status,owner_approved_at,
                   human_live_gate_required,research_only,live_money_execution_allowed
            FROM provider_governance_policies
            WHERE policy_version=:policy
            """
        ),
        {"policy": POLICY_VERSION},
    ).mappings().first()
    if row is None:
        raise RuntimeError("day14_governance_policy_missing")
    if not bool(row["research_only"]) or bool(row["live_money_execution_allowed"]):
        raise RuntimeError("day14_policy_research_boundary_invalid")
    if not bool(row["human_live_gate_required"]):
        raise RuntimeError("day14_human_live_gate_missing")
    return row


def _latest_day13_run(session: Any) -> Mapping[str, Any]:
    row = session.execute(
        text(
            """
            SELECT id,code_sha,evidence_digest,completed_at,research_only,
                   live_money_execution_allowed,authoritative_discovery_count
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


def _frozen_boundary(session: Any) -> datetime:
    row = session.execute(
        text(
            """
            SELECT MIN(preregistered_at) AS first_boundary,
                   MAX(preregistered_at) AS last_boundary,
                   COUNT(*) AS hypotheses
            FROM provider_conditional_hypotheses
            WHERE registry_version=:registry
            """
        ),
        {"registry": day13.REGISTRY_VERSION},
    ).mappings().one()
    if int(row["hypotheses"] or 0) == 0 or row["first_boundary"] != row["last_boundary"]:
        raise RuntimeError("day13_preregistration_boundary_not_frozen")
    return row["first_boundary"].astimezone(UTC)


def _shadow_profiles(session: Any) -> list[Mapping[str, Any]]:
    return list(
        session.execute(
            text(
                """
                SELECT s.id AS source_id,prp.research_state,prp.duplicate_of_source_id
                FROM sources s
                JOIN provider_research_profiles prp ON prp.source_id=s.id
                WHERE s.status='shadow'
                ORDER BY s.id
                """
            )
        ).mappings().all()
    )


def _prior_result(session: Any, source_id: UUID) -> Mapping[str, Any] | None:
    return session.execute(
        text(
            """
            SELECT x.evidence_state,x.oos_trade_count,x.provider_evidence_digest,
                   x.research_state_transitioned,x.current_research_state,x.proposed_research_state
            FROM provider_governance_results x
            JOIN provider_governance_runs r ON r.id=x.run_id
            WHERE x.source_id=:source_id AND r.completed_at IS NOT NULL
            ORDER BY r.completed_at DESC,x.created_at DESC LIMIT 1
            """
        ),
        {"source_id": source_id},
    ).mappings().first()


def _provider_evidence(
    *,
    source_id: UUID,
    observations: list[day13.ConditionalObservation],
    conditional_results: list[dict[str, Any]],
    frozen_boundary: datetime,
    policy: Mapping[str, Any],
) -> ProviderEvidence:
    source_observations = [
        observation
        for observation in observations
        if observation.source_id == source_id and observation.signal_posted_at > frozen_boundary
    ]
    # One realized R per trade. Defensive de-duplication keeps a future join expansion from
    # accidentally weighting the same shadow trade more than once.
    realized_by_trade = {observation.trade_id: observation.realized_r for observation in source_observations}
    values = list(realized_by_trade.values())
    source_results = [row for row in conditional_results if UUID(str(row["source_id"])) == source_id]
    tested = sum(bool(row["minimum_oos_gate_met"]) for row in source_results)
    positive = sum(
        bool(row["builder_gate_candidate"]) and row["shrunken_effect_r"] is not None and float(row["shrunken_effect_r"]) > 0
        for row in source_results
    )
    negative = sum(
        bool(row["builder_gate_candidate"]) and row["shrunken_effect_r"] is not None and float(row["shrunken_effect_r"]) < 0
        for row in source_results
    )
    state, mean, lower, upper = classify_performance(
        values,
        minimum_oos_n=int(policy["minimum_oos_n"]),
        tested_fingerprint_cells=tested,
        minimum_tested_fingerprint_cells=int(policy["minimum_tested_fingerprint_cells"]),
    )
    payload = {
        "source_id": str(source_id),
        "trade_ids": sorted(str(value) for value in realized_by_trade),
        "r_values": [round(float(realized_by_trade[key]), 8) for key in sorted(realized_by_trade, key=str)],
        "tested_fingerprint_cells": tested,
        "positive_candidates": positive,
        "negative_candidates": negative,
        "frozen_boundary": frozen_boundary.isoformat(),
        "policy_version": str(policy["policy_version"]),
    }
    return ProviderEvidence(
        oos_trade_count=len(values),
        mean_quality_r=mean,
        lower_95_r=lower,
        upper_95_r=upper,
        tested_fingerprint_cell_count=tested,
        positive_candidate_count=positive,
        negative_candidate_count=negative,
        evidence_state=state,
        evidence_digest=_digest(payload),
    )


def _governance_status(policy: Mapping[str, Any], evidence: list[ProviderEvidence]) -> str:
    if str(policy["approval_status"]) != OWNER_APPROVED:
        return "WAITING-FOR-OWNER-THRESHOLD-APPROVAL"
    if not any(row.oos_trade_count >= int(policy["minimum_oos_n"]) for row in evidence):
        return "WAITING-FOR-FORWARD-EVIDENCE"
    return "RESEARCH-GOVERNANCE-ACTIVE"


def run() -> dict[str, Any]:
    session_factory = get_session_factory()
    code_sha = _code_sha()
    cutoff = datetime.now(UTC)
    lock = get_engine().connect()
    acquired = bool(lock.execute(text("SELECT pg_try_advisory_lock(hashtext('provider_day14_governance_v1'))")).scalar_one())
    if not acquired:
        lock.close()
        return {"engineering_status": "SKIPPED_LOCK_HELD", "research_only": True, "live_money_execution_allowed": False}

    try:
        with session_factory() as session:
            policy = _load_policy(session)
            source_run = _latest_day13_run(session)
            source_run_id = UUID(str(source_run["id"]))
            frozen_boundary = _frozen_boundary(session)
            profiles = _shadow_profiles(session)

            hypotheses = day13._load_registry(session)
            observations = day13_runtime._load_observations(session, cutoff=cutoff)
            conditional_results, _eligible_trade_ids = day13.build_conditional_results(hypotheses, observations)

            evidence_by_source: dict[UUID, ProviderEvidence] = {}
            for profile in profiles:
                source_id = UUID(str(profile["source_id"]))
                evidence_by_source[source_id] = _provider_evidence(
                    source_id=source_id,
                    observations=observations,
                    conditional_results=conditional_results,
                    frozen_boundary=frozen_boundary,
                    policy=policy,
                )

            global_evidence_payload = {
                "policy_version": POLICY_VERSION,
                "policy_approval_status": str(policy["approval_status"]),
                "frozen_boundary": frozen_boundary.isoformat(),
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
            evidence_digest = _digest(global_evidence_payload)
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
                {"policy": POLICY_VERSION, "model": MODEL_VERSION, "digest": evidence_digest},
            ).mappings().first()
            if existing:
                return {
                    "run_id": str(existing["id"]),
                    "engineering_status": existing["engineering_status"],
                    "governance_status": existing["governance_status"],
                    "evidence_digest": existing["evidence_digest"],
                    "skipped_unchanged_evidence": True,
                    "research_only": True,
                    "live_money_execution_allowed": False,
                }

            simulation = simulated_governance_acceptance()
            engineering_status = "ENGINEERING_PROVEN" if simulation["acceptance_passed"] else "FAILED_SIMULATION_ACCEPTANCE"
            decisions: list[dict[str, Any]] = []
            for profile in profiles:
                source_id = UUID(str(profile["source_id"]))
                current_state = str(profile["research_state"])
                evidence = evidence_by_source[source_id]
                prior = _prior_result(session, source_id)
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
                duplicate = profile["duplicate_of_source_id"] is not None or current_state == "duplicate_review"
                decision = decide_governance(
                    current_research_state=current_state,
                    policy_approval_status=str(policy["approval_status"]),
                    evidence_state=evidence.evidence_state,
                    sustained_decay=sustained_decay,
                    duplicate_review=duplicate,
                    fresh_evidence_since_transition=fresh,
                )
                transition = decision.proposed_research_state != current_state and decision.proposed_action in {"PROMOTE", "DEMOTE"}
                if transition:
                    changed = session.execute(
                        text(
                            """
                            UPDATE provider_research_profiles
                            SET research_state=:new_state,updated_at=now()
                            WHERE source_id=:source_id AND research_state=:current_state
                            """
                        ),
                        {"new_state": decision.proposed_research_state, "source_id": source_id, "current_state": current_state},
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
                        "sustained_decay": sustained_decay,
                        "duplicate_review": duplicate,
                        "paper_qualified": decision.paper_qualified,
                        "research_state_transitioned": transition,
                        "reason": decision.reason,
                    }
                )

            governance_status = _governance_status(policy, list(evidence_by_source.values()))
            counts = {action: sum(row["proposed_action"] == action for row in decisions) for action in ACTIONS}
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
                    "policy": POLICY_VERSION,
                    "source_run": source_run_id,
                    "model": MODEL_VERSION,
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
                    "simulation": json.dumps(simulation, sort_keys=True),
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
                                    "frozen_oos_boundary": frozen_boundary.isoformat(),
                                },
                                sort_keys=True,
                            ),
                        }
                        for row in decisions
                    ],
                )

            session.execute(text("UPDATE provider_governance_runs SET completed_at=now() WHERE id=:id"), {"id": run_id})
            session.commit()

            summary = {
                "run_id": str(run_id),
                "model_version": MODEL_VERSION,
                "policy_version": POLICY_VERSION,
                "policy_approval_status": str(policy["approval_status"]),
                "code_sha": code_sha,
                "source_day13_run_id": str(source_run_id),
                "frozen_oos_boundary": frozen_boundary.isoformat(),
                "engineering_status": engineering_status,
                "governance_status": governance_status,
                "provider_count": len(decisions),
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
            print("PROVIDER_DAY14_GOVERNANCE=" + json.dumps(summary, sort_keys=True), flush=True)
            return summary
    finally:
        try:
            lock.execute(text("SELECT pg_advisory_unlock(hashtext('provider_day14_governance_v1'))"))
        finally:
            lock.close()


def run_forever() -> None:
    interval = max(3600, int(os.getenv("SUPER_SIGNALS_PROVIDER_GOVERNANCE_SECONDS", "21600") or "21600"))
    while True:
        try:
            run()
        except Exception as exc:
            print("PROVIDER_DAY14_GOVERNANCE_ERROR=" + type(exc).__name__, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
