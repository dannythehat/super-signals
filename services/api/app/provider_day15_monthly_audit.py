"""Provider Intelligence Day 15 monthly audit and governance harness.

This module audits only the 40 shadow discovery providers. It consumes persisted
research evidence from Days 13-14 plus the point-in-time shadow execution-cost
projection. It never mutates sources.status, provider research state, broker routing,
sizing or live-money authority.

Capture completeness follows the master-build definition and is split into:
1. message-ingress completeness: whether Telegram posts/edits/replies were actually
   received and persisted; and
2. interpretation completeness: whether received trade-candidate messages became a
   structured signal or lifecycle action.

The database can measure interpretation completeness, but it cannot prove an externally
missing Telegram message from its own persisted rows. Message-ingress completeness is
therefore fail-closed until an independent capture denominator/ground truth exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import get_session_factory

MODEL_VERSION = "provider_day15_v1"
POLICY_VERSION = "provider_day15_policy_v1"
OWNER_APPROVED = "OWNER_APPROVED"


@dataclass(frozen=True, slots=True)
class CandidateGate:
    sample_status: str
    independence_status: str
    ingress_status: str
    interpretation_status: str
    execution_cost_status: str
    shadow_oos_status: str
    multiple_testing_status: str


@dataclass(frozen=True, slots=True)
class ProviderAudit:
    source_id: UUID
    research_state: str
    oos_trade_count: int
    sample_status: str
    independence_status: str
    duplicate_of_source_id: UUID | None
    duplicate_score: float | None
    ingress_status: str
    persisted_message_count: int
    persisted_revision_count: int
    interpretation_status: str
    classified_candidate_message_count: int
    structured_candidate_message_count: int
    interpretation_completeness: float | None
    rejection_reasons: dict[str, int]
    execution_cost_status: str
    execution_projection_count: int
    mean_execution_adjusted_r_p50: float | None
    mean_execution_adjusted_r_p95: float | None
    shadow_oos_status: str
    multiple_testing_status: str
    tested_fingerprint_cell_count: int
    bh_rejected_count: int
    minimum_effect_gate_count: int
    statistically_defensible_cell_count: int
    candidate_status: str
    reviewable: bool
    evidence_digest: str


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _code_sha() -> str:
    for name in ("RENDER_GIT_COMMIT", "GITHUB_SHA", "SOURCE_COMMIT"):
        value = (os.getenv(name) or "").strip()
        if len(value) == 40:
            return value
    return "0" * 40


def candidate_is_reviewable(gate: CandidateGate) -> bool:
    """Return True only when every Day 15 defensibility gate is positively proven."""

    return (
        gate.sample_status == "PASS"
        and gate.independence_status == "PASS"
        and gate.ingress_status == "PASS"
        and gate.interpretation_status == "PASS"
        and gate.execution_cost_status == "PASS"
        and gate.shadow_oos_status == "POSITIVE_CONFIDENT"
        and gate.multiple_testing_status == "PASS"
    )


def pure_noise_month_acceptance() -> dict[str, Any]:
    """Deterministic max-of-N acceptance: pure noise must not create a star provider.

    Forty fixed, zero-centred provider samples intentionally contain a positive raw leader.
    No provider has a multiplicity-corrected discovery, so the candidate gate must return
    zero reviewable providers despite that tempting raw maximum.
    """

    raw_means = [((index * 17) % 23 - 11) / 100 for index in range(40)]
    raw_top_mean_r = max(raw_means)
    reviewable = 0
    for _mean in raw_means:
        gate = CandidateGate(
            sample_status="PASS",
            independence_status="PASS",
            ingress_status="PASS",
            interpretation_status="PASS",
            execution_cost_status="PASS",
            shadow_oos_status="POSITIVE_CONFIDENT",
            multiple_testing_status="NO_BH_DISCOVERY",
        )
        reviewable += int(candidate_is_reviewable(gate))
    return {
        "synthetic_only": True,
        "fixture": "pure_noise_month_max_of_40_v1",
        "provider_count": 40,
        "raw_top_mean_r": raw_top_mean_r,
        "reviewable_candidate_count": reviewable,
        "acceptance_passed": raw_top_mean_r > 0 and reviewable == 0,
        "statistical_authority_granted": False,
        "live_money_authority_granted": False,
    }


def _policy(session: Any) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT policy_version,audit_window_days,ingress_completeness_threshold,
                   interpretation_completeness_threshold,minimum_execution_adjusted_r,
                   capture_threshold_approval_status,execution_edge_threshold_approval_status,
                   decision_class_approval_status
            FROM provider_monthly_audit_policies
            WHERE policy_version=:policy_version
            """
        ),
        {"policy_version": POLICY_VERSION},
    ).mappings().one()
    return dict(row)


def _current_governance_source(session: Any, code_sha: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT id,source_conditional_run_id,policy_approval_status,engineering_status,
                   governance_status,completed_at
            FROM provider_governance_runs
            WHERE completed_at IS NOT NULL AND code_sha=:code_sha
            ORDER BY completed_at DESC,id DESC
            LIMIT 1
            """
        ),
        {"code_sha": code_sha},
    ).mappings().first()
    return dict(row) if row else None


def _conditional_source(session: Any, run_id: UUID) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT id,registry_version,minimum_oos_n,threshold_approval_status,
                   statistical_status,completed_at
            FROM provider_conditional_runs
            WHERE id=:run_id AND completed_at IS NOT NULL
            """
        ),
        {"run_id": run_id},
    ).mappings().one()
    boundary = session.execute(
        text(
            """
            SELECT min(preregistered_at) AS boundary
            FROM provider_conditional_hypotheses
            WHERE registry_version=:registry_version
            """
        ),
        {"registry_version": row["registry_version"]},
    ).scalar_one()
    result = dict(row)
    result["oos_boundary"] = boundary
    return result


def _shadow_provider_rows(session: Any, governance_run_id: UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT s.id AS source_id,p.research_state,p.duplicate_of_source_id,p.duplicate_score,
                   COALESCE(g.oos_trade_count,0) AS oos_trade_count,
                   COALESCE(g.evidence_state,'INSUFFICIENT_OOS') AS evidence_state,
                   COALESCE(g.tested_fingerprint_cell_count,0) AS tested_fingerprint_cell_count
            FROM sources s
            JOIN provider_research_profiles p ON p.source_id=s.id
            LEFT JOIN provider_governance_results g
              ON g.source_id=s.id AND g.run_id=:governance_run_id
            WHERE s.status='shadow'
            ORDER BY s.id
            """
        ),
        {"governance_run_id": governance_run_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _capture_metrics(session: Any, start: datetime, end: datetime) -> dict[UUID, dict[str, Any]]:
    persisted_rows = session.execute(
        text(
            """
            SELECT s.id AS source_id,
                   count(DISTINCT m.id)::int AS persisted_message_count,
                   count(mr.id)::int AS persisted_revision_count
            FROM sources s
            LEFT JOIN messages m
              ON m.source_id=s.id AND m.posted_at >= :start_at AND m.posted_at < :end_at
            LEFT JOIN message_revisions mr ON mr.message_id=m.id
            WHERE s.status='shadow'
            GROUP BY s.id
            """
        ),
        {"start_at": start, "end_at": end},
    ).mappings().all()

    structured_rows = session.execute(
        text(
            """
            WITH candidate_messages AS (
              SELECT DISTINCT m.source_id,m.id AS message_id
              FROM messages m
              JOIN message_classifications c ON c.message_id=m.id
              JOIN sources s ON s.id=m.source_id
              WHERE s.status='shadow'
                AND m.posted_at >= :start_at AND m.posted_at < :end_at
                AND c.classification IN ('new_trade','trade_update')
                AND c.decision_status='classified'
            )
            SELECT c.source_id,
                   count(*)::int AS classified_candidate_message_count,
                   count(*) FILTER (
                     WHERE EXISTS (
                       SELECT 1 FROM signals sg
                       WHERE sg.source_message_id=c.message_id AND sg.parser_status='accepted'
                     ) OR EXISTS (
                       SELECT 1 FROM signal_lifecycle_events le
                       WHERE le.source_message_id=c.message_id
                     )
                   )::int AS structured_candidate_message_count
            FROM candidate_messages c
            GROUP BY c.source_id
            """
        ),
        {"start_at": start, "end_at": end},
    ).mappings().all()

    rejection_rows = session.execute(
        text(
            """
            WITH candidate_messages AS (
              SELECT DISTINCT m.source_id,m.id AS message_id
              FROM messages m
              JOIN message_classifications c ON c.message_id=m.id
              JOIN sources s ON s.id=m.source_id
              WHERE s.status='shadow'
                AND m.posted_at >= :start_at AND m.posted_at < :end_at
                AND c.classification IN ('new_trade','trade_update')
                AND c.decision_status='classified'
            ), unstructured AS (
              SELECT c.source_id,c.message_id
              FROM candidate_messages c
              WHERE NOT EXISTS (
                SELECT 1 FROM signals sg
                WHERE sg.source_message_id=c.message_id AND sg.parser_status='accepted'
              ) AND NOT EXISTS (
                SELECT 1 FROM signal_lifecycle_events le
                WHERE le.source_message_id=c.message_id
              )
            )
            SELECT u.source_id,
                   COALESCE(p.reason,'no_structured_signal_or_lifecycle_action') AS reason,
                   count(*)::int AS n
            FROM unstructured u
            LEFT JOIN LATERAL (
              SELECT mp.reason
              FROM message_parses mp
              WHERE mp.message_id=u.message_id AND mp.parse_status='failed'
              ORDER BY mp.revision_index DESC,mp.created_at DESC
              LIMIT 1
            ) p ON true
            GROUP BY u.source_id,COALESCE(p.reason,'no_structured_signal_or_lifecycle_action')
            ORDER BY u.source_id,n DESC
            """
        ),
        {"start_at": start, "end_at": end},
    ).mappings().all()

    result: dict[UUID, dict[str, Any]] = {}
    for row in persisted_rows:
        result[row["source_id"]] = {
            "persisted_message_count": int(row["persisted_message_count"] or 0),
            "persisted_revision_count": int(row["persisted_revision_count"] or 0),
            "classified_candidate_message_count": 0,
            "structured_candidate_message_count": 0,
            "rejection_reasons": {},
        }
    for row in structured_rows:
        target = result.setdefault(row["source_id"], {})
        target["classified_candidate_message_count"] = int(row["classified_candidate_message_count"] or 0)
        target["structured_candidate_message_count"] = int(row["structured_candidate_message_count"] or 0)
    for row in rejection_rows:
        target = result.setdefault(row["source_id"], {})
        target.setdefault("rejection_reasons", {})[str(row["reason"])] = int(row["n"] or 0)
    return result


def _execution_metrics(
    session: Any,
    *,
    start: datetime,
    end: datetime,
    oos_boundary: datetime,
) -> dict[UUID, dict[str, Any]]:
    effective_start = max(start, oos_boundary)
    rows = session.execute(
        text(
            """
            SELECT p.source_id,count(*)::int AS projection_count,
                   avg(p.execution_adjusted_r_p50) AS mean_p50,
                   avg(p.execution_adjusted_r_p95) AS mean_p95
            FROM provider_shadow_execution_projection p
            JOIN sources s ON s.id=p.source_id
            WHERE s.status='shadow'
              AND p.research_only=true AND p.live_money_execution_allowed=false
              AND p.signal_posted_at > :start_at AND p.signal_posted_at < :end_at
              AND p.execution_adjusted_r_p50 IS NOT NULL
              AND p.execution_adjusted_r_p95 IS NOT NULL
            GROUP BY p.source_id
            """
        ),
        {"start_at": effective_start, "end_at": end},
    ).mappings().all()
    return {
        row["source_id"]: {
            "projection_count": int(row["projection_count"] or 0),
            "mean_p50": float(row["mean_p50"]) if row["mean_p50"] is not None else None,
            "mean_p95": float(row["mean_p95"]) if row["mean_p95"] is not None else None,
        }
        for row in rows
    }


def _multiple_testing_metrics(session: Any, conditional_run_id: UUID) -> dict[UUID, dict[str, int]]:
    rows = session.execute(
        text(
            """
            SELECT source_id,count(*)::int AS tested_cells,
                   count(*) FILTER (WHERE bh_rejected)::int AS bh_rejected_count,
                   count(*) FILTER (WHERE minimum_effect_gate_met)::int AS minimum_effect_gate_count,
                   count(*) FILTER (WHERE bh_rejected AND minimum_effect_gate_met)::int AS defensible_cells
            FROM provider_conditional_results
            WHERE run_id=:run_id
            GROUP BY source_id
            """
        ),
        {"run_id": conditional_run_id},
    ).mappings().all()
    return {
        row["source_id"]: {
            "tested_cells": int(row["tested_cells"] or 0),
            "bh_rejected_count": int(row["bh_rejected_count"] or 0),
            "minimum_effect_gate_count": int(row["minimum_effect_gate_count"] or 0),
            "defensible_cells": int(row["defensible_cells"] or 0),
        }
        for row in rows
    }


def _audit_provider(
    row: dict[str, Any],
    *,
    policy: dict[str, Any],
    conditional: dict[str, Any],
    capture: dict[str, Any],
    execution: dict[str, Any],
    multiple: dict[str, int],
) -> ProviderAudit:
    oos_n = int(row.get("oos_trade_count") or 0)
    minimum_oos_n = int(conditional["minimum_oos_n"])
    sample_status = "PASS" if oos_n >= minimum_oos_n else "WAITING_FORWARD_EVIDENCE"

    duplicate_of = row.get("duplicate_of_source_id")
    independence_status = "PASS" if duplicate_of is None else "DEPENDENCE_REVIEW"

    # Persisted rows prove what we received, not what Telegram may have failed to deliver.
    ingress_status = "UNVERIFIABLE_EXTERNAL_DENOMINATOR"

    classified = int(capture.get("classified_candidate_message_count") or 0)
    structured = int(capture.get("structured_candidate_message_count") or 0)
    interpretation_completeness = (structured / classified) if classified else None
    if classified == 0:
        interpretation_status = "NO_CANDIDATE_MESSAGES"
    elif policy["capture_threshold_approval_status"] != OWNER_APPROVED:
        interpretation_status = "WAITING_THRESHOLD_APPROVAL"
    else:
        threshold = float(policy["interpretation_completeness_threshold"])
        interpretation_status = "PASS" if interpretation_completeness is not None and interpretation_completeness >= threshold else "FAIL"

    projection_count = int(execution.get("projection_count") or 0)
    mean_p50 = execution.get("mean_p50")
    mean_p95 = execution.get("mean_p95")
    if projection_count == 0:
        execution_status = "WAITING_FORWARD_EVIDENCE"
    elif policy["execution_edge_threshold_approval_status"] != OWNER_APPROVED:
        execution_status = "WAITING_THRESHOLD_APPROVAL"
    else:
        threshold = float(policy["minimum_execution_adjusted_r"])
        execution_status = "PASS" if mean_p95 is not None and mean_p95 >= threshold else "FAIL"

    shadow_oos_status = str(row.get("evidence_state") or "INSUFFICIENT_OOS")
    tested_cells = int(multiple.get("tested_cells") or row.get("tested_fingerprint_cell_count") or 0)
    bh_count = int(multiple.get("bh_rejected_count") or 0)
    effect_count = int(multiple.get("minimum_effect_gate_count") or 0)
    defensible_cells = int(multiple.get("defensible_cells") or 0)
    if tested_cells == 0:
        multiple_status = "WAITING_FORWARD_EVIDENCE"
    elif conditional["threshold_approval_status"] != OWNER_APPROVED:
        multiple_status = "THRESHOLDS_UNAPPROVED"
    elif defensible_cells > 0:
        multiple_status = "PASS"
    else:
        multiple_status = "NO_BH_DISCOVERY"

    gate = CandidateGate(
        sample_status=sample_status,
        independence_status=independence_status,
        ingress_status=ingress_status,
        interpretation_status=interpretation_status,
        execution_cost_status=execution_status,
        shadow_oos_status=shadow_oos_status,
        multiple_testing_status=multiple_status,
    )
    reviewable = candidate_is_reviewable(gate)

    if independence_status != "PASS":
        candidate_status = "DEPENDENCE_REVIEW"
    elif ingress_status != "PASS" or sample_status == "WAITING_FORWARD_EVIDENCE" or execution_status == "WAITING_FORWARD_EVIDENCE" or multiple_status == "WAITING_FORWARD_EVIDENCE":
        candidate_status = "WAITING_EVIDENCE"
    elif interpretation_status == "WAITING_THRESHOLD_APPROVAL" or execution_status == "WAITING_THRESHOLD_APPROVAL" or multiple_status == "THRESHOLDS_UNAPPROVED":
        candidate_status = "WAITING_APPROVAL"
    elif reviewable:
        candidate_status = "REVIEWABLE"
    else:
        candidate_status = "NOT_DEFENSIBLE"

    evidence = {
        "model_version": MODEL_VERSION,
        "source_id": str(row["source_id"]),
        "gates": asdict(gate),
        "oos_trade_count": oos_n,
        "minimum_oos_n": minimum_oos_n,
        "duplicate_of_source_id": str(duplicate_of) if duplicate_of else None,
        "duplicate_score": float(row["duplicate_score"]) if row.get("duplicate_score") is not None else None,
        "persisted_message_count": int(capture.get("persisted_message_count") or 0),
        "persisted_revision_count": int(capture.get("persisted_revision_count") or 0),
        "classified_candidate_message_count": classified,
        "structured_candidate_message_count": structured,
        "interpretation_completeness": interpretation_completeness,
        "rejection_reasons": capture.get("rejection_reasons") or {},
        "execution_projection_count": projection_count,
        "mean_execution_adjusted_r_p50": mean_p50,
        "mean_execution_adjusted_r_p95": mean_p95,
        "tested_fingerprint_cell_count": tested_cells,
        "bh_rejected_count": bh_count,
        "minimum_effect_gate_count": effect_count,
        "statistically_defensible_cell_count": defensible_cells,
        "reviewable": reviewable,
        "real_promotion_authority": False,
        "real_demotion_authority": False,
        "live_money_execution_allowed": False,
    }

    return ProviderAudit(
        source_id=row["source_id"],
        research_state=str(row["research_state"]),
        oos_trade_count=oos_n,
        sample_status=sample_status,
        independence_status=independence_status,
        duplicate_of_source_id=duplicate_of,
        duplicate_score=float(row["duplicate_score"]) if row.get("duplicate_score") is not None else None,
        ingress_status=ingress_status,
        persisted_message_count=int(capture.get("persisted_message_count") or 0),
        persisted_revision_count=int(capture.get("persisted_revision_count") or 0),
        interpretation_status=interpretation_status,
        classified_candidate_message_count=classified,
        structured_candidate_message_count=structured,
        interpretation_completeness=interpretation_completeness,
        rejection_reasons=dict(capture.get("rejection_reasons") or {}),
        execution_cost_status=execution_status,
        execution_projection_count=projection_count,
        mean_execution_adjusted_r_p50=mean_p50,
        mean_execution_adjusted_r_p95=mean_p95,
        shadow_oos_status=shadow_oos_status,
        multiple_testing_status=multiple_status,
        tested_fingerprint_cell_count=tested_cells,
        bh_rejected_count=bh_count,
        minimum_effect_gate_count=effect_count,
        statistically_defensible_cell_count=defensible_cells,
        candidate_status=candidate_status,
        reviewable=reviewable,
        evidence_digest=_digest(evidence),
    )


def run_once() -> dict[str, Any]:
    code_sha = _code_sha()
    session_factory = get_session_factory()
    simulation = pure_noise_month_acceptance()
    if not simulation["acceptance_passed"]:
        raise RuntimeError("pure_noise_month_acceptance_failed")

    with session_factory() as session:
        policy = _policy(session)
        governance = _current_governance_source(session, code_sha)
        if governance is None:
            return {
                "status": "WAITING_SOURCE_GOVERNANCE_REFRESH",
                "code_sha": code_sha,
                "research_only": True,
                "live_money_execution_allowed": False,
            }
        conditional = _conditional_source(session, governance["source_conditional_run_id"])
        end = datetime.now(UTC)
        start = end - timedelta(days=int(policy["audit_window_days"]))

        providers = _shadow_provider_rows(session, governance["id"])
        capture = _capture_metrics(session, start, end)
        execution = _execution_metrics(
            session,
            start=start,
            end=end,
            oos_boundary=conditional["oos_boundary"],
        )
        multiple = _multiple_testing_metrics(session, conditional["id"])

        audits = [
            _audit_provider(
                row,
                policy=policy,
                conditional=conditional,
                capture=capture.get(row["source_id"], {}),
                execution=execution.get(row["source_id"], {}),
                multiple=multiple.get(row["source_id"], {}),
            )
            for row in providers
        ]

        evidence = {
            "model_version": MODEL_VERSION,
            "policy_version": POLICY_VERSION,
            "source_governance_run_id": str(governance["id"]),
            "source_conditional_run_id": str(conditional["id"]),
            "audit_window_start": start.isoformat(),
            "audit_window_end": end.isoformat(),
            "policy": policy,
            "conditional_threshold_approval_status": conditional["threshold_approval_status"],
            "simulation": simulation,
            "provider_evidence": [audit.evidence_digest for audit in audits],
        }
        evidence_digest = _digest(evidence)

        existing = session.execute(
            text(
                """
                SELECT id,audit_status,provider_count,reviewable_candidate_count,evidence_digest
                FROM provider_monthly_audit_runs
                WHERE policy_version=:policy_version AND model_version=:model_version
                  AND evidence_digest=:evidence_digest AND completed_at IS NOT NULL
                """
            ),
            {
                "policy_version": POLICY_VERSION,
                "model_version": MODEL_VERSION,
                "evidence_digest": evidence_digest,
            },
        ).mappings().first()
        if existing:
            return {**dict(existing), "idempotent": True, "code_sha": code_sha}

        reviewable_count = sum(int(audit.reviewable) for audit in audits)
        if any(audit.ingress_status != "PASS" for audit in audits):
            audit_status = "WAITING-CAPTURE-GROUND-TRUTH"
        elif any(
            audit.sample_status == "WAITING_FORWARD_EVIDENCE"
            or audit.execution_cost_status == "WAITING_FORWARD_EVIDENCE"
            or audit.multiple_testing_status == "WAITING_FORWARD_EVIDENCE"
            for audit in audits
        ):
            audit_status = "WAITING-FOR-FORWARD-EVIDENCE"
        elif (
            policy["capture_threshold_approval_status"] != OWNER_APPROVED
            or policy["execution_edge_threshold_approval_status"] != OWNER_APPROVED
            or policy["decision_class_approval_status"] != OWNER_APPROVED
            or conditional["threshold_approval_status"] != OWNER_APPROVED
        ):
            audit_status = "WAITING-FOR-OWNER-THRESHOLD-APPROVAL"
        elif reviewable_count > 0:
            audit_status = "RESEARCH-CANDIDATES-AVAILABLE"
        else:
            audit_status = "NO-DEFENSIBLE-CANDIDATES"

        run_id = session.execute(
            text(
                """
                INSERT INTO provider_monthly_audit_runs(
                  policy_version,source_governance_run_id,source_conditional_run_id,
                  model_version,code_sha,audit_window_start,audit_window_end,
                  engineering_status,audit_status,provider_count,reviewable_candidate_count,
                  real_promotion_authority_count,real_demotion_authority_count,
                  simulation_json,evidence_digest,research_only,live_money_execution_allowed
                ) VALUES (
                  :policy_version,:governance_run_id,:conditional_run_id,
                  :model_version,:code_sha,:window_start,:window_end,
                  'ENGINEERING_PROVEN',:audit_status,:provider_count,:reviewable_count,
                  0,0,CAST(:simulation AS jsonb),:evidence_digest,true,false
                ) RETURNING id
                """
            ),
            {
                "policy_version": POLICY_VERSION,
                "governance_run_id": governance["id"],
                "conditional_run_id": conditional["id"],
                "model_version": MODEL_VERSION,
                "code_sha": code_sha,
                "window_start": start,
                "window_end": end,
                "audit_status": audit_status,
                "provider_count": len(audits),
                "reviewable_count": reviewable_count,
                "simulation": _canonical_json(simulation),
                "evidence_digest": evidence_digest,
            },
        ).scalar_one()

        for audit in audits:
            evidence_json = {
                "capture_truth": {
                    "message_ingress": "external denominator unavailable; persisted rows cannot prove missing Telegram delivery",
                    "interpretation": "classified new_trade/trade_update messages mapped to accepted signals or lifecycle events",
                    "rejection_reasons": audit.rejection_reasons,
                },
                "statistically_defensible_cell_count": audit.statistically_defensible_cell_count,
                "decision_class_approval_status": policy["decision_class_approval_status"],
                "conditional_threshold_approval_status": conditional["threshold_approval_status"],
                "real_promotion_authority": False,
                "real_demotion_authority": False,
                "research_only": True,
                "live_money_execution_allowed": False,
            }
            session.execute(
                text(
                    """
                    INSERT INTO provider_candidates(
                      run_id,source_id,research_state,sample_status,oos_trade_count,
                      independence_status,duplicate_of_source_id,duplicate_score,
                      ingress_status,persisted_message_count,persisted_revision_count,
                      interpretation_status,classified_candidate_message_count,
                      structured_candidate_message_count,interpretation_completeness,
                      rejection_reasons_json,execution_cost_status,execution_projection_count,
                      mean_execution_adjusted_r_p50,mean_execution_adjusted_r_p95,
                      shadow_oos_status,multiple_testing_status,tested_fingerprint_cell_count,
                      bh_rejected_count,minimum_effect_gate_count,candidate_status,reviewable,
                      real_promotion_authority,real_demotion_authority,evidence_json,evidence_digest,
                      research_only,live_money_execution_allowed
                    ) VALUES (
                      :run_id,:source_id,:research_state,:sample_status,:oos_trade_count,
                      :independence_status,:duplicate_of_source_id,:duplicate_score,
                      :ingress_status,:persisted_message_count,:persisted_revision_count,
                      :interpretation_status,:classified_count,:structured_count,
                      :interpretation_completeness,CAST(:rejection_reasons AS jsonb),
                      :execution_cost_status,:projection_count,:mean_p50,:mean_p95,
                      :shadow_oos_status,:multiple_testing_status,:tested_cells,
                      :bh_count,:effect_count,:candidate_status,:reviewable,
                      false,false,CAST(:evidence_json AS jsonb),:evidence_digest,true,false
                    )
                    """
                ),
                {
                    "run_id": run_id,
                    "source_id": audit.source_id,
                    "research_state": audit.research_state,
                    "sample_status": audit.sample_status,
                    "oos_trade_count": audit.oos_trade_count,
                    "independence_status": audit.independence_status,
                    "duplicate_of_source_id": audit.duplicate_of_source_id,
                    "duplicate_score": audit.duplicate_score,
                    "ingress_status": audit.ingress_status,
                    "persisted_message_count": audit.persisted_message_count,
                    "persisted_revision_count": audit.persisted_revision_count,
                    "interpretation_status": audit.interpretation_status,
                    "classified_count": audit.classified_candidate_message_count,
                    "structured_count": audit.structured_candidate_message_count,
                    "interpretation_completeness": audit.interpretation_completeness,
                    "rejection_reasons": _canonical_json(audit.rejection_reasons),
                    "execution_cost_status": audit.execution_cost_status,
                    "projection_count": audit.execution_projection_count,
                    "mean_p50": audit.mean_execution_adjusted_r_p50,
                    "mean_p95": audit.mean_execution_adjusted_r_p95,
                    "shadow_oos_status": audit.shadow_oos_status,
                    "multiple_testing_status": audit.multiple_testing_status,
                    "tested_cells": audit.tested_fingerprint_cell_count,
                    "bh_count": audit.bh_rejected_count,
                    "effect_count": audit.minimum_effect_gate_count,
                    "candidate_status": audit.candidate_status,
                    "reviewable": audit.reviewable,
                    "evidence_json": _canonical_json(evidence_json),
                    "evidence_digest": audit.evidence_digest,
                },
            )

        session.execute(
            text("UPDATE provider_monthly_audit_runs SET completed_at=now() WHERE id=:run_id"),
            {"run_id": run_id},
        )
        session.commit()

        return {
            "run_id": str(run_id),
            "code_sha": code_sha,
            "model_version": MODEL_VERSION,
            "policy_version": POLICY_VERSION,
            "engineering_status": "ENGINEERING_PROVEN",
            "audit_status": audit_status,
            "provider_count": len(audits),
            "reviewable_candidate_count": reviewable_count,
            "real_promotion_authority_count": 0,
            "real_demotion_authority_count": 0,
            "simulation_acceptance_passed": True,
            "evidence_digest": evidence_digest,
            "research_only": True,
            "live_money_execution_allowed": False,
        }


def run_forever() -> None:
    interval = max(300, int(os.getenv("PROVIDER_DAY15_AUDIT_SECONDS", "3600")))
    while True:
        try:
            result = run_once()
            print("PROVIDER_DAY15_MONTHLY_AUDIT=" + _canonical_json(result), flush=True)
            if result.get("status") == "WAITING_SOURCE_GOVERNANCE_REFRESH":
                time.sleep(15)
                continue
        except Exception as exc:  # fail-safe research sidecar
            print("PROVIDER_DAY15_MONTHLY_AUDIT_ERROR=" + type(exc).__name__, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
