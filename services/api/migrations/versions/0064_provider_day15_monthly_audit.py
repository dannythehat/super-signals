"""Add Provider Intelligence Day 15 monthly audit and candidate governance.

Revision ID: 0064_provider_day15_monthly_audit
Revises: 0063_provider_day14_governance
Create Date: 2026-09-07

Day 15 is a research-only monthly audit harness. It creates review candidates only after
sample sufficiency, source independence, capture completeness, execution-cost-adjusted
edge, shadow OOS and multiple-testing gates are defensible. It cannot promote/demote a
real provider, mutate sources.status, route broker orders or grant live-money authority.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064_provider_day15_monthly_audit"
down_revision: str | None = "0063_provider_day14_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_monthly_audit_policies",
        sa.Column("policy_version", sa.String(length=64), primary_key=True),
        sa.Column("audit_window_days", sa.Integer(), nullable=False),
        sa.Column("ingress_completeness_threshold", sa.Numeric(8, 6), nullable=True),
        sa.Column("interpretation_completeness_threshold", sa.Numeric(8, 6), nullable=True),
        sa.Column("minimum_execution_adjusted_r", sa.Numeric(14, 8), nullable=True),
        sa.Column("capture_threshold_approval_status", sa.String(length=40), nullable=False),
        sa.Column("execution_edge_threshold_approval_status", sa.String(length=40), nullable=False),
        sa.Column("decision_class_approval_status", sa.String(length=40), nullable=False),
        sa.Column("owner_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("audit_window_days >= 1", name="ck_provider_monthly_audit_policy_window"),
        sa.CheckConstraint(
            "capture_threshold_approval_status IN ('UNSET_UNAPPROVED','OWNER_APPROVED')",
            name="ck_provider_monthly_audit_capture_approval",
        ),
        sa.CheckConstraint(
            "execution_edge_threshold_approval_status IN ('UNSET_UNAPPROVED','OWNER_APPROVED')",
            name="ck_provider_monthly_audit_edge_approval",
        ),
        sa.CheckConstraint(
            "decision_class_approval_status IN ('PROPOSED_UNAPPROVED','OWNER_APPROVED')",
            name="ck_provider_monthly_audit_decision_approval",
        ),
        sa.CheckConstraint(
            "capture_threshold_approval_status <> 'OWNER_APPROVED' OR "
            "(ingress_completeness_threshold IS NOT NULL AND interpretation_completeness_threshold IS NOT NULL)",
            name="ck_provider_monthly_audit_capture_threshold_present",
        ),
        sa.CheckConstraint(
            "execution_edge_threshold_approval_status <> 'OWNER_APPROVED' OR minimum_execution_adjusted_r IS NOT NULL",
            name="ck_provider_monthly_audit_edge_threshold_present",
        ),
        sa.CheckConstraint("research_only", name="ck_provider_monthly_audit_policy_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_monthly_audit_policy_no_live_money"),
    )
    op.execute(
        """
        INSERT INTO provider_monthly_audit_policies(
          policy_version,audit_window_days,capture_threshold_approval_status,
          execution_edge_threshold_approval_status,decision_class_approval_status,
          research_only,live_money_execution_allowed
        ) VALUES (
          'provider_day15_policy_v1',30,'UNSET_UNAPPROVED','UNSET_UNAPPROVED',
          'PROPOSED_UNAPPROVED',true,false
        )
        """
    )

    op.create_table(
        "provider_monthly_audit_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("policy_version", sa.String(length=64), sa.ForeignKey("provider_monthly_audit_policies.policy_version"), nullable=False),
        sa.Column("source_governance_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_governance_runs.id"), nullable=False),
        sa.Column("source_conditional_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_conditional_runs.id"), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("code_sha", sa.String(length=40), nullable=False),
        sa.Column("audit_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engineering_status", sa.String(length=48), nullable=False, server_default="RUNNING"),
        sa.Column("audit_status", sa.String(length=72), nullable=False),
        sa.Column("provider_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reviewable_candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("real_promotion_authority_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("real_demotion_authority_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("simulation_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("failure_reason", sa.String(length=240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("policy_version", "model_version", "evidence_digest", name="uq_provider_monthly_audit_run_evidence"),
        sa.CheckConstraint("audit_window_start < audit_window_end", name="ck_provider_monthly_audit_window_order"),
        sa.CheckConstraint(
            "audit_status IN ('WAITING-CAPTURE-GROUND-TRUTH','WAITING-FOR-FORWARD-EVIDENCE',"
            "'WAITING-FOR-OWNER-THRESHOLD-APPROVAL','RESEARCH-CANDIDATES-AVAILABLE')",
            name="ck_provider_monthly_audit_status",
        ),
        sa.CheckConstraint(
            "provider_count >= 0 AND reviewable_candidate_count >= 0 "
            "AND real_promotion_authority_count = 0 AND real_demotion_authority_count = 0",
            name="ck_provider_monthly_audit_authority_counts",
        ),
        sa.CheckConstraint("research_only", name="ck_provider_monthly_audit_run_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_monthly_audit_run_no_live_money"),
    )

    op.create_table(
        "provider_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_monthly_audit_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("research_state", sa.String(length=24), nullable=False),
        sa.Column("sample_status", sa.String(length=48), nullable=False),
        sa.Column("oos_trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("independence_status", sa.String(length=48), nullable=False),
        sa.Column("duplicate_of_source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duplicate_score", sa.Numeric(10, 8), nullable=True),
        sa.Column("ingress_status", sa.String(length=64), nullable=False),
        sa.Column("persisted_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("persisted_revision_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interpretation_status", sa.String(length=64), nullable=False),
        sa.Column("classified_candidate_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("structured_candidate_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interpretation_completeness", sa.Numeric(10, 8), nullable=True),
        sa.Column("rejection_reasons_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("execution_cost_status", sa.String(length=64), nullable=False),
        sa.Column("execution_projection_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mean_execution_adjusted_r_p50", sa.Numeric(14, 8), nullable=True),
        sa.Column("mean_execution_adjusted_r_p95", sa.Numeric(14, 8), nullable=True),
        sa.Column("shadow_oos_status", sa.String(length=48), nullable=False),
        sa.Column("multiple_testing_status", sa.String(length=56), nullable=False),
        sa.Column("tested_fingerprint_cell_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bh_rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minimum_effect_gate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_status", sa.String(length=48), nullable=False),
        sa.Column("reviewable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("real_promotion_authority", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("real_demotion_authority", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("run_id", "source_id", name="uq_provider_candidate_run_source"),
        sa.CheckConstraint("oos_trade_count >= 0 AND persisted_message_count >= 0 AND persisted_revision_count >= 0", name="ck_provider_candidate_nonnegative_a"),
        sa.CheckConstraint("classified_candidate_message_count >= 0 AND structured_candidate_message_count >= 0 AND execution_projection_count >= 0", name="ck_provider_candidate_nonnegative_b"),
        sa.CheckConstraint("tested_fingerprint_cell_count >= 0 AND bh_rejected_count >= 0 AND minimum_effect_gate_count >= 0", name="ck_provider_candidate_nonnegative_c"),
        sa.CheckConstraint("NOT real_promotion_authority", name="ck_provider_candidate_no_promotion_authority"),
        sa.CheckConstraint("NOT real_demotion_authority", name="ck_provider_candidate_no_demotion_authority"),
        sa.CheckConstraint("research_only", name="ck_provider_candidate_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_candidate_no_live_money"),
    )
    op.create_index("ix_provider_candidates_source", "provider_candidates", ["source_id", "run_id"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_provider_candidate_mutation()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'provider candidate audit evidence is append-only';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_provider_candidate_append_only
        BEFORE UPDATE OR DELETE ON provider_candidates
        FOR EACH ROW EXECUTE FUNCTION prevent_provider_candidate_mutation();
        """
    )

    op.execute(
        """
        CREATE VIEW provider_candidates_latest AS
        WITH latest AS (
          SELECT id FROM provider_monthly_audit_runs
          WHERE completed_at IS NOT NULL
          ORDER BY completed_at DESC,id DESC LIMIT 1
        )
        SELECT r.id AS run_id,r.policy_version,r.model_version,r.code_sha,
               r.source_governance_run_id,r.source_conditional_run_id,
               r.audit_window_start,r.audit_window_end,r.engineering_status,r.audit_status,
               r.provider_count,r.reviewable_candidate_count,
               0 AS real_promotion_authority_count,0 AS real_demotion_authority_count,
               r.simulation_json,r.evidence_digest AS run_evidence_digest,
               c.source_id,COALESCE(s.chat_title,s.source_alias) AS provider_title,
               c.research_state,c.sample_status,c.oos_trade_count,c.independence_status,
               c.duplicate_of_source_id,c.duplicate_score,c.ingress_status,
               c.persisted_message_count,c.persisted_revision_count,c.interpretation_status,
               c.classified_candidate_message_count,c.structured_candidate_message_count,
               c.interpretation_completeness,c.rejection_reasons_json,
               c.execution_cost_status,c.execution_projection_count,
               c.mean_execution_adjusted_r_p50,c.mean_execution_adjusted_r_p95,
               c.shadow_oos_status,c.multiple_testing_status,c.tested_fingerprint_cell_count,
               c.bh_rejected_count,c.minimum_effect_gate_count,c.candidate_status,c.reviewable,
               false AS real_promotion_authority,false AS real_demotion_authority,
               c.evidence_json,c.evidence_digest,true AS research_only,
               false AS live_money_execution_allowed
        FROM latest l
        JOIN provider_monthly_audit_runs r ON r.id=l.id
        LEFT JOIN provider_candidates c ON c.run_id=r.id
        LEFT JOIN sources s ON s.id=c.source_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_candidates_latest")
    op.execute("DROP TRIGGER IF EXISTS trg_provider_candidate_append_only ON provider_candidates")
    op.execute("DROP FUNCTION IF EXISTS prevent_provider_candidate_mutation()")
    op.drop_index("ix_provider_candidates_source", table_name="provider_candidates")
    op.drop_table("provider_candidates")
    op.drop_table("provider_monthly_audit_runs")
    op.drop_table("provider_monthly_audit_policies")
