"""Add Provider Intelligence Day 14 promotion/demotion governance.

Revision ID: 0063_provider_day14_governance
Revises: 0062_provider_day13_conditional
Create Date: 2026-09-07

Day 14 wires evidence-backed governance into the existing provider_research_profiles
learning -> shadow -> qualified state machine. `qualified` is paper-research eligibility
only. This migration creates no path to change sources.status or grant live-money authority.
Promotion thresholds are builder recommendations and remain unapproved until an explicit
owner decision records approval.
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
        "provider_governance_policies",
        sa.Column("policy_version", sa.String(length=64), primary_key=True),
        sa.Column("minimum_oos_n", sa.Integer(), nullable=False),
        sa.Column("minimum_tested_fingerprint_cells", sa.Integer(), nullable=False),
        sa.Column("confidence_level", sa.Numeric(6, 5), nullable=False),
        sa.Column("oos_window_mode", sa.String(length=64), nullable=False),
        sa.Column("approval_status", sa.String(length=40), nullable=False),
        sa.Column("owner_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("human_live_gate_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("minimum_oos_n >= 1", name="ck_provider_governance_policy_min_n"),
        sa.CheckConstraint("minimum_tested_fingerprint_cells >= 1", name="ck_provider_governance_policy_min_fingerprint_cells"),
        sa.CheckConstraint("confidence_level > 0 AND confidence_level < 1", name="ck_provider_governance_policy_confidence"),
        sa.CheckConstraint("oos_window_mode = 'since_day13_preregistration'", name="ck_provider_governance_policy_oos_window"),
        sa.CheckConstraint("approval_status IN ('PROPOSED_UNAPPROVED','OWNER_APPROVED')", name="ck_provider_governance_policy_approval"),
        sa.CheckConstraint(
            "(approval_status='PROPOSED_UNAPPROVED' AND owner_approved_at IS NULL) OR "
            "(approval_status='OWNER_APPROVED' AND owner_approved_at IS NOT NULL)",
            name="ck_provider_governance_policy_approval_timestamp",
        ),
        sa.CheckConstraint("human_live_gate_required", name="ck_provider_governance_policy_human_gate"),
        sa.CheckConstraint("research_only", name="ck_provider_governance_policy_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_governance_policy_no_live_money"),
    )
    op.execute(
        """
        INSERT INTO provider_governance_policies(
            policy_version,minimum_oos_n,minimum_tested_fingerprint_cells,confidence_level,
            oos_window_mode,approval_status,human_live_gate_required,research_only,
            live_money_execution_allowed
        ) VALUES (
            'provider_day14_policy_v1',30,1,0.95,'since_day13_preregistration',
            'PROPOSED_UNAPPROVED',true,true,false
        )
        """
    )

    op.create_table(
        "provider_governance_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("policy_version", sa.String(length=64), sa.ForeignKey("provider_governance_policies.policy_version"), nullable=False),
        sa.Column("source_conditional_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_conditional_runs.id"), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("code_sha", sa.String(length=40), nullable=False),
        sa.Column("policy_approval_status", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engineering_status", sa.String(length=48), nullable=False, server_default="RUNNING"),
        sa.Column("governance_status", sa.String(length=64), nullable=False),
        sa.Column("provider_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hold_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("promotion_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("demotion_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retest_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("research_state_transition_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paper_qualified_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("authoritative_live_transition_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("simulation_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("policy_version", "model_version", "evidence_digest", name="uq_provider_governance_run_evidence"),
        sa.CheckConstraint("policy_approval_status IN ('PROPOSED_UNAPPROVED','OWNER_APPROVED')", name="ck_provider_governance_run_policy_approval"),
        sa.CheckConstraint(
            "governance_status IN ('WAITING-FOR-FORWARD-EVIDENCE','WAITING-FOR-OWNER-THRESHOLD-APPROVAL','RESEARCH-GOVERNANCE-ACTIVE')",
            name="ck_provider_governance_run_status",
        ),
        sa.CheckConstraint(
            "provider_count >= 0 AND hold_count >= 0 AND promotion_count >= 0 "
            "AND demotion_count >= 0 AND retest_count >= 0 AND research_state_transition_count >= 0 "
            "AND paper_qualified_count >= 0 AND authoritative_live_transition_count = 0",
            name="ck_provider_governance_run_counts",
        ),
        sa.CheckConstraint("research_only", name="ck_provider_governance_run_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_governance_run_no_live_money"),
    )

    op.create_table(
        "provider_governance_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_governance_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("current_research_state", sa.String(length=24), nullable=False),
        sa.Column("proposed_research_state", sa.String(length=24), nullable=False),
        sa.Column("proposed_action", sa.String(length=16), nullable=False),
        sa.Column("evidence_state", sa.String(length=40), nullable=False),
        sa.Column("oos_trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mean_quality_r", sa.Numeric(14, 8), nullable=True),
        sa.Column("lower_95_r", sa.Numeric(14, 8), nullable=True),
        sa.Column("upper_95_r", sa.Numeric(14, 8), nullable=True),
        sa.Column("tested_fingerprint_cell_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("positive_candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("negative_candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("sustained_decay", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("duplicate_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("paper_qualified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("human_live_gate_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("research_state_transitioned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("authoritative_live_transition", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("run_id", "source_id", name="uq_provider_governance_result_source"),
        sa.CheckConstraint(
            "current_research_state IN ('learning','shadow','duplicate_review','qualified','rejected') "
            "AND proposed_research_state IN ('learning','shadow','duplicate_review','qualified','rejected')",
            name="ck_provider_governance_result_states",
        ),
        sa.CheckConstraint("proposed_action IN ('HOLD','PROMOTE','DEMOTE','RETEST')", name="ck_provider_governance_result_action"),
        sa.CheckConstraint(
            "evidence_state IN ('INSUFFICIENT_OOS','FINGERPRINT_NOT_READY','POSITIVE_CONFIDENT','NEGATIVE_CONFIDENT','UNCERTAIN')",
            name="ck_provider_governance_result_evidence",
        ),
        sa.CheckConstraint(
            "oos_trade_count >= 0 AND tested_fingerprint_cell_count >= 0 "
            "AND positive_candidate_count >= 0 AND negative_candidate_count >= 0",
            name="ck_provider_governance_result_counts",
        ),
        sa.CheckConstraint("human_live_gate_required", name="ck_provider_governance_result_human_gate"),
        sa.CheckConstraint("NOT authoritative_live_transition", name="ck_provider_governance_result_no_live_authority"),
        sa.CheckConstraint("research_only", name="ck_provider_governance_result_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_governance_result_no_live_money"),
    )
    op.create_index("ix_provider_governance_results_source", "provider_governance_results", ["source_id", "run_id"])

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
        SELECT r.id AS run_id,r.model_version,r.code_sha,r.policy_version,
               r.source_conditional_run_id,r.policy_approval_status,r.engineering_status,
               r.governance_status,r.provider_count,r.hold_count,r.promotion_count,
               r.demotion_count,r.retest_count,r.research_state_transition_count,
               r.paper_qualified_count,0 AS authoritative_live_transition_count,
               r.simulation_json,r.evidence_digest,x.source_id,
               COALESCE(s.chat_title,s.source_alias) AS provider_title,
               x.current_research_state,x.proposed_research_state,x.proposed_action,
               x.evidence_state,x.oos_trade_count,x.mean_quality_r,x.lower_95_r,x.upper_95_r,
               x.tested_fingerprint_cell_count,x.positive_candidate_count,
               x.negative_candidate_count,x.provider_evidence_digest,x.sustained_decay,x.duplicate_review,
               x.paper_qualified,x.human_live_gate_required,x.research_state_transitioned,
               false AS authoritative_live_transition,x.reason_json,
               true AS research_only,false AS live_money_execution_allowed
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
    op.drop_table("provider_governance_policies")
