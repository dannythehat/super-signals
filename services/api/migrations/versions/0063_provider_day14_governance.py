"""Add Provider Intelligence Day 14 research governance state machine.

Revision ID: 0063_provider_day14_governance
Revises: 0062_provider_day13_conditional
Create Date: 2026-09-07

Day 14 governs research-stage promotion/demotion/re-test proposals only. It cannot mutate
source execution status, route trades, size positions, approve statistical thresholds or
grant live-money authority. The terminal stage in this subsystem is tiny_live_candidate;
actual live activation requires a separate explicit owner-gated mechanism.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0063_provider_day14_governance"
down_revision: str | None = "0062_provider_day13_conditional"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_governance_states",
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("rollout_stage", sa.String(length=32), nullable=False, server_default="shadow"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_transition_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint(
            "rollout_stage IN ('shadow','supervised','paper_candidate','tiny_live_candidate')",
            name="ck_provider_governance_state_stage",
        ),
        sa.CheckConstraint("state_version >= 1", name="ck_provider_governance_state_version"),
        sa.CheckConstraint("research_only", name="ck_provider_governance_state_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_governance_state_no_live_money"),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_provider_governance_shadow_source()
        RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM sources s WHERE s.id = NEW.source_id AND s.status = 'shadow'
          ) THEN
            RAISE EXCEPTION 'provider governance state is shadow-provider research only';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_provider_governance_shadow_source
        BEFORE INSERT OR UPDATE ON provider_governance_states
        FOR EACH ROW EXECUTE FUNCTION enforce_provider_governance_shadow_source();
        """
    )

    op.create_table(
        "provider_governance_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "source_conditional_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_conditional_runs.id"),
            nullable=False,
        ),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("code_sha", sa.String(length=40), nullable=False),
        sa.Column("source_statistical_status", sa.String(length=64), nullable=False),
        sa.Column("source_threshold_approval_status", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engineering_status", sa.String(length=48), nullable=False, server_default="RUNNING"),
        sa.Column("governance_status", sa.String(length=64), nullable=False),
        sa.Column("provider_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hold_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("promotion_proposal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("demotion_proposal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retest_proposal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("research_stage_transition_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("human_review_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("authoritative_transition_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("simulation_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint(
            "source_conditional_run_id", "model_version", "code_sha",
            name="uq_provider_governance_run_source_model_sha",
        ),
        sa.CheckConstraint(
            "governance_status IN ('WAITING-FOR-FORWARD-EVIDENCE','WAITING-FOR-THRESHOLD-APPROVAL','RESEARCH-PROPOSALS-READY')",
            name="ck_provider_governance_run_status",
        ),
        sa.CheckConstraint(
            "provider_count >= 0 AND hold_count >= 0 AND promotion_proposal_count >= 0 "
            "AND demotion_proposal_count >= 0 AND retest_proposal_count >= 0 "
            "AND research_stage_transition_count >= 0 AND human_review_count >= 0 "
            "AND authoritative_transition_count = 0",
            name="ck_provider_governance_run_counts",
        ),
        sa.CheckConstraint("research_only", name="ck_provider_governance_run_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_governance_run_no_live_money"),
    )

    op.create_table(
        "provider_governance_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_governance_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("research_profile_state", sa.String(length=24), nullable=False),
        sa.Column("current_rollout_stage", sa.String(length=32), nullable=False),
        sa.Column("proposed_rollout_stage", sa.String(length=32), nullable=False),
        sa.Column("proposed_action", sa.String(length=16), nullable=False),
        sa.Column("evidence_state", sa.String(length=40), nullable=False),
        sa.Column("positive_candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("negative_candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("human_gate_status", sa.String(length=40), nullable=False),
        sa.Column("eligible_for_human_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("research_stage_transitioned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("authoritative_transition", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("run_id", "source_id", name="uq_provider_governance_result_source"),
        sa.CheckConstraint(
            "research_profile_state IN ('learning','shadow','duplicate_review','qualified','rejected')",
            name="ck_provider_governance_result_profile_state",
        ),
        sa.CheckConstraint(
            "current_rollout_stage IN ('shadow','supervised','paper_candidate','tiny_live_candidate') "
            "AND proposed_rollout_stage IN ('shadow','supervised','paper_candidate','tiny_live_candidate')",
            name="ck_provider_governance_result_stages",
        ),
        sa.CheckConstraint(
            "proposed_action IN ('HOLD','PROMOTE','DEMOTE','RETEST')",
            name="ck_provider_governance_result_action",
        ),
        sa.CheckConstraint(
            "evidence_state IN ('WAITING_FORWARD_EVIDENCE','WAITING_THRESHOLD_APPROVAL','DUPLICATE_REVIEW',"
            "'VALIDATED_POSITIVE','VALIDATED_NEGATIVE','CONFLICTING_EVIDENCE','NO_VALIDATED_EDGE','DRIFTED')",
            name="ck_provider_governance_result_evidence",
        ),
        sa.CheckConstraint(
            "human_gate_status IN ('NOT_ELIGIBLE','REQUIRED_BEFORE_LIVE')",
            name="ck_provider_governance_result_human_gate",
        ),
        sa.CheckConstraint(
            "positive_candidate_count >= 0 AND negative_candidate_count >= 0",
            name="ck_provider_governance_result_candidate_counts",
        ),
        sa.CheckConstraint("NOT authoritative_transition", name="ck_provider_governance_result_no_authority"),
        sa.CheckConstraint("research_only", name="ck_provider_governance_result_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_governance_result_no_live_money"),
    )
    op.create_index(
        "ix_provider_governance_results_source",
        "provider_governance_results",
        ["source_id", "run_id"],
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_provider_governance_result_mutation()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'provider governance result evidence is append-only';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_provider_governance_result_append_only
        BEFORE UPDATE OR DELETE ON provider_governance_results
        FOR EACH ROW EXECUTE FUNCTION prevent_provider_governance_result_mutation();
        """
    )

    op.execute(
        """
        CREATE VIEW provider_governance_latest AS
        WITH latest AS (
          SELECT id FROM provider_governance_runs
          WHERE completed_at IS NOT NULL
          ORDER BY completed_at DESC,id DESC LIMIT 1
        )
        SELECT r.id AS run_id,r.model_version,r.code_sha,r.source_conditional_run_id,
               r.source_statistical_status,r.source_threshold_approval_status,
               r.engineering_status,r.governance_status,r.provider_count,r.hold_count,
               r.promotion_proposal_count,r.demotion_proposal_count,r.retest_proposal_count,
               r.research_stage_transition_count,r.human_review_count,
               0 AS authoritative_transition_count,r.simulation_json,r.evidence_digest,
               x.source_id,COALESCE(s.chat_title,s.source_alias) AS provider_title,
               x.research_profile_state,x.current_rollout_stage,x.proposed_rollout_stage,
               x.proposed_action,x.evidence_state,x.positive_candidate_count,
               x.negative_candidate_count,x.human_gate_status,x.eligible_for_human_review,
               x.research_stage_transitioned,false AS authoritative_transition,
               x.reason_json,true AS research_only,false AS live_money_execution_allowed
        FROM latest l
        JOIN provider_governance_runs r ON r.id=l.id
        LEFT JOIN provider_governance_results x ON x.run_id=r.id
        LEFT JOIN sources s ON s.id=x.source_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_governance_latest")
    op.execute("DROP TRIGGER IF EXISTS trg_provider_governance_result_append_only ON provider_governance_results")
    op.execute("DROP FUNCTION IF EXISTS prevent_provider_governance_result_mutation()")
    op.drop_index("ix_provider_governance_results_source", table_name="provider_governance_results")
    op.drop_table("provider_governance_results")
    op.drop_table("provider_governance_runs")
    op.execute("DROP TRIGGER IF EXISTS trg_provider_governance_shadow_source ON provider_governance_states")
    op.execute("DROP FUNCTION IF EXISTS enforce_provider_governance_shadow_source()")
    op.drop_table("provider_governance_states")
