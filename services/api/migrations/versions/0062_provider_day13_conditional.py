"""Add Provider Intelligence Day 13 conditional/FDR evidence surfaces.

Revision ID: 0062_provider_day13_conditional
Revises: 0061_provider_day12_fingerprint
Create Date: 2026-09-07

Day 13 preregisters conditional hypotheses before confirmatory evidence exists.
Thresholds are builder proposals only. Statistical/live-money authority is hard-walled off.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0062_provider_day13_conditional"
down_revision: str | None = "0061_provider_day12_fingerprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_conditional_hypotheses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("registry_version", sa.String(length=64), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("session_bucket", sa.String(length=32), nullable=False),
        sa.Column("regime_definition_version", sa.String(length=64), nullable=False),
        sa.Column("regime_dimension", sa.String(length=40), nullable=False),
        sa.Column("regime_value", sa.String(length=64), nullable=False),
        sa.Column("duration_bucket", sa.String(length=32), nullable=False),
        sa.Column("duration_semantics", sa.String(length=64), nullable=False, server_default="realized_descriptive_post_entry"),
        sa.Column("primary_metric", sa.String(length=40), nullable=False, server_default="quality_r_multiple"),
        sa.Column("minimum_oos_n", sa.Integer(), nullable=False),
        sa.Column("proposed_fdr_q", sa.Numeric(8, 6), nullable=False),
        sa.Column("proposed_min_effect_r", sa.Numeric(10, 6), nullable=False),
        sa.Column("threshold_approval_status", sa.String(length=40), nullable=False, server_default="PROPOSED_UNAPPROVED"),
        sa.Column("oos_boundary", sa.String(length=40), nullable=False, server_default="preregistered_at"),
        sa.Column("preregistered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preregistration_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint(
            "registry_version", "source_id", "side", "session_bucket", "regime_dimension",
            "regime_value", "duration_bucket", name="uq_provider_conditional_hypothesis"
        ),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="ck_provider_conditional_hypothesis_side"),
        sa.CheckConstraint("session_bucket IN ('asia','europe','ny_early','other')", name="ck_provider_conditional_hypothesis_session"),
        sa.CheckConstraint(
            "regime_dimension IN ('trend_structure','volatility_band','quote_spread_condition','event_timing')",
            name="ck_provider_conditional_hypothesis_regime_dimension",
        ),
        sa.CheckConstraint(
            "duration_bucket IN ('lt_15m','15m_to_60m','1h_to_4h','gte_4h')",
            name="ck_provider_conditional_hypothesis_duration",
        ),
        sa.CheckConstraint("duration_semantics = 'realized_descriptive_post_entry'", name="ck_provider_conditional_hypothesis_duration_semantics"),
        sa.CheckConstraint("primary_metric = 'quality_r_multiple'", name="ck_provider_conditional_hypothesis_metric"),
        sa.CheckConstraint("minimum_oos_n >= 1", name="ck_provider_conditional_hypothesis_min_n"),
        sa.CheckConstraint("proposed_fdr_q > 0 AND proposed_fdr_q <= 1", name="ck_provider_conditional_hypothesis_fdr"),
        sa.CheckConstraint("proposed_min_effect_r >= 0", name="ck_provider_conditional_hypothesis_effect"),
        sa.CheckConstraint("threshold_approval_status = 'PROPOSED_UNAPPROVED'", name="ck_provider_conditional_hypothesis_unapproved"),
        sa.CheckConstraint("oos_boundary = 'preregistered_at'", name="ck_provider_conditional_hypothesis_oos"),
        sa.CheckConstraint("research_only", name="ck_provider_conditional_hypothesis_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_conditional_hypothesis_no_live_money"),
    )
    op.create_index(
        "ix_provider_conditional_hypothesis_source",
        "provider_conditional_hypotheses",
        ["source_id", "registry_version", "preregistered_at"],
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_provider_conditional_hypothesis_mutation()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'provider conditional preregistration is append-only';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_provider_conditional_hypothesis_append_only
        BEFORE UPDATE OR DELETE ON provider_conditional_hypotheses
        FOR EACH ROW EXECUTE FUNCTION prevent_provider_conditional_hypothesis_mutation();
        """
    )

    op.create_table(
        "provider_conditional_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("registry_version", sa.String(length=64), nullable=False),
        sa.Column("code_sha", sa.String(length=40), nullable=False),
        sa.Column("evidence_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("minimum_oos_n", sa.Integer(), nullable=False),
        sa.Column("proposed_fdr_q", sa.Numeric(8, 6), nullable=False),
        sa.Column("proposed_min_effect_r", sa.Numeric(10, 6), nullable=False),
        sa.Column("threshold_approval_status", sa.String(length=40), nullable=False, server_default="PROPOSED_UNAPPROVED"),
        sa.Column("fdr_family", sa.String(length=64), nullable=False, server_default="provider_day13_v1_global"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engineering_status", sa.String(length=48), nullable=False, server_default="RUNNING"),
        sa.Column("statistical_status", sa.String(length=64), nullable=False, server_default="WAITING-FOR-FORWARD-EVIDENCE"),
        sa.Column("provider_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("preregistered_hypothesis_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible_oos_trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tested_hypothesis_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bh_rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("builder_gate_candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("authoritative_discovery_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("simulation_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("model_version", "code_sha", name="uq_provider_conditional_run_model_sha"),
        sa.CheckConstraint("minimum_oos_n >= 1", name="ck_provider_conditional_run_min_n"),
        sa.CheckConstraint("proposed_fdr_q > 0 AND proposed_fdr_q <= 1", name="ck_provider_conditional_run_fdr"),
        sa.CheckConstraint("proposed_min_effect_r >= 0", name="ck_provider_conditional_run_effect"),
        sa.CheckConstraint("threshold_approval_status = 'PROPOSED_UNAPPROVED'", name="ck_provider_conditional_run_unapproved"),
        sa.CheckConstraint("fdr_family = 'provider_day13_v1_global'", name="ck_provider_conditional_run_family"),
        sa.CheckConstraint("statistical_status = 'WAITING-FOR-FORWARD-EVIDENCE'", name="ck_provider_conditional_run_waiting"),
        sa.CheckConstraint(
            "provider_count >= 0 AND preregistered_hypothesis_count >= 0 AND eligible_oos_trade_count >= 0 "
            "AND result_count >= 0 AND tested_hypothesis_count >= 0 AND bh_rejected_count >= 0 "
            "AND builder_gate_candidate_count >= 0 AND authoritative_discovery_count = 0",
            name="ck_provider_conditional_run_counts",
        ),
        sa.CheckConstraint("research_only", name="ck_provider_conditional_run_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_conditional_run_no_live_money"),
    )

    op.create_table(
        "provider_conditional_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_conditional_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("hypothesis_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_conditional_hypotheses.id"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("cell_oos_n", sa.Integer(), nullable=False),
        sa.Column("complement_oos_n", sa.Integer(), nullable=False),
        sa.Column("raw_cell_mean_r", sa.Numeric(14, 8), nullable=True),
        sa.Column("raw_complement_mean_r", sa.Numeric(14, 8), nullable=True),
        sa.Column("raw_effect_r", sa.Numeric(14, 8), nullable=True),
        sa.Column("shrunken_effect_r", sa.Numeric(14, 8), nullable=True),
        sa.Column("p_value", sa.Numeric(14, 12), nullable=True),
        sa.Column("bh_adjusted_p", sa.Numeric(14, 12), nullable=True),
        sa.Column("bh_rejected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("minimum_oos_gate_met", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("minimum_effect_gate_met", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("builder_gate_candidate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("authoritative_discovery", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("posterior_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("descriptive_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("threshold_approval_status", sa.String(length=40), nullable=False, server_default="PROPOSED_UNAPPROVED"),
        sa.Column("statistical_status", sa.String(length=64), nullable=False, server_default="WAITING-FOR-FORWARD-EVIDENCE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("run_id", "hypothesis_id", name="uq_provider_conditional_result_hypothesis"),
        sa.CheckConstraint("cell_oos_n >= 0 AND complement_oos_n >= 0", name="ck_provider_conditional_result_n"),
        sa.CheckConstraint("p_value IS NULL OR (p_value >= 0 AND p_value <= 1)", name="ck_provider_conditional_result_p"),
        sa.CheckConstraint("bh_adjusted_p IS NULL OR (bh_adjusted_p >= 0 AND bh_adjusted_p <= 1)", name="ck_provider_conditional_result_bh_p"),
        sa.CheckConstraint("threshold_approval_status = 'PROPOSED_UNAPPROVED'", name="ck_provider_conditional_result_unapproved"),
        sa.CheckConstraint("statistical_status = 'WAITING-FOR-FORWARD-EVIDENCE'", name="ck_provider_conditional_result_waiting"),
        sa.CheckConstraint("NOT authoritative_discovery", name="ck_provider_conditional_result_no_authority"),
        sa.CheckConstraint("research_only", name="ck_provider_conditional_result_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_conditional_result_no_live_money"),
    )
    op.create_index(
        "ix_provider_conditional_results_source",
        "provider_conditional_results",
        ["source_id", "run_id"],
    )

    op.execute(
        """
        CREATE VIEW provider_conditional_latest AS
        WITH latest AS (
            SELECT id FROM provider_conditional_runs
            WHERE completed_at IS NOT NULL
            ORDER BY completed_at DESC,id DESC LIMIT 1
        )
        SELECT r.id AS run_id,r.model_version,r.registry_version,r.code_sha,r.evidence_cutoff,
               r.minimum_oos_n,r.proposed_fdr_q,r.proposed_min_effect_r,
               r.threshold_approval_status,r.fdr_family,r.engineering_status,
               r.statistical_status AS run_statistical_status,r.provider_count,
               r.preregistered_hypothesis_count,r.eligible_oos_trade_count,r.result_count,
               r.tested_hypothesis_count,r.bh_rejected_count,r.builder_gate_candidate_count,
               r.authoritative_discovery_count,r.simulation_json,r.evidence_digest,
               h.source_id,COALESCE(s.chat_title,s.source_alias) AS provider_title,
               h.side,h.session_bucket,h.regime_definition_version,h.regime_dimension,
               h.regime_value,h.duration_bucket,h.duration_semantics,h.preregistered_at,
               x.cell_oos_n,x.complement_oos_n,x.raw_cell_mean_r,x.raw_complement_mean_r,
               x.raw_effect_r,x.shrunken_effect_r,x.p_value,x.bh_adjusted_p,x.bh_rejected,
               x.minimum_oos_gate_met,x.minimum_effect_gate_met,x.builder_gate_candidate,
               false AS authoritative_discovery,x.posterior_json,x.descriptive_json,
               x.statistical_status,true AS research_only,false AS live_money_execution_allowed
        FROM latest l
        JOIN provider_conditional_runs r ON r.id=l.id
        LEFT JOIN provider_conditional_results x ON x.run_id=r.id
        LEFT JOIN provider_conditional_hypotheses h ON h.id=x.hypothesis_id
        LEFT JOIN sources s ON s.id=h.source_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_conditional_latest")
    op.drop_index("ix_provider_conditional_results_source", table_name="provider_conditional_results")
    op.drop_table("provider_conditional_results")
    op.drop_table("provider_conditional_runs")
    op.execute("DROP TRIGGER IF EXISTS trg_provider_conditional_hypothesis_append_only ON provider_conditional_hypotheses")
    op.execute("DROP FUNCTION IF EXISTS prevent_provider_conditional_hypothesis_mutation()")
    op.drop_index("ix_provider_conditional_hypothesis_source", table_name="provider_conditional_hypotheses")
    op.drop_table("provider_conditional_hypotheses")
