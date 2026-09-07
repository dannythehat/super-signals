"""Add Provider Intelligence Day 12 fingerprint/statistics evidence surfaces.

Revision ID: 0061_provider_day12_fingerprint
Revises: 0060_provider_day11_reconcile
Create Date: 2026-09-07

Day 12 is a dormant research harness. These tables can never grant broker, sizing,
provider-routing or live-money authority. Statistical authority remains fail-closed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061_provider_day12_fingerprint"
down_revision: str | None = "0060_provider_day11_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_fingerprint_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("code_sha", sa.String(length=40), nullable=False),
        sa.Column("evidence_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("minimum_forward_n", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engineering_status", sa.String(length=48), nullable=False, server_default="RUNNING"),
        sa.Column("statistical_status", sa.String(length=64), nullable=False, server_default="WAITING-FOR-FORWARD-EVIDENCE"),
        sa.Column("provider_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible_trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cell_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("model_version", "code_sha", name="uq_provider_fingerprint_run_model_sha"),
        sa.CheckConstraint("minimum_forward_n >= 1", name="ck_provider_fingerprint_run_min_n"),
        sa.CheckConstraint("provider_count >= 0 AND eligible_trade_count >= 0 AND cell_count >= 0", name="ck_provider_fingerprint_run_counts"),
        sa.CheckConstraint("statistical_status = 'WAITING-FOR-FORWARD-EVIDENCE'", name="ck_provider_fingerprint_stat_authority_waiting"),
        sa.CheckConstraint("research_only", name="ck_provider_fingerprint_run_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_fingerprint_run_no_live_money"),
    )

    op.create_table(
        "provider_fingerprint_cells",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_fingerprint_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("dimension_kind", sa.String(length=40), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=True),
        sa.Column("session_bucket", sa.String(length=32), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("n_gate_met", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("primary_estimate_kind", sa.String(length=48), nullable=False, server_default="posterior_partial_pool"),
        sa.Column("posterior_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("descriptive_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("statistical_status", sa.String(length=64), nullable=False, server_default="WAITING-FOR-FORWARD-EVIDENCE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("run_id", "source_id", "dimension_kind", "side", "session_bucket", name="uq_provider_fingerprint_cell_dimension"),
        sa.CheckConstraint("dimension_kind IN ('provider','provider_direction','provider_session','provider_direction_session')", name="ck_provider_fingerprint_cell_dimension"),
        sa.CheckConstraint("side IS NULL OR side IN ('BUY','SELL')", name="ck_provider_fingerprint_cell_side"),
        sa.CheckConstraint("sample_count >= 1", name="ck_provider_fingerprint_cell_n"),
        sa.CheckConstraint("primary_estimate_kind = 'posterior_partial_pool'", name="ck_provider_fingerprint_cell_primary"),
        sa.CheckConstraint("statistical_status = 'WAITING-FOR-FORWARD-EVIDENCE'", name="ck_provider_fingerprint_cell_waiting"),
        sa.CheckConstraint("research_only", name="ck_provider_fingerprint_cell_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_fingerprint_cell_no_live_money"),
    )
    op.create_index("ix_provider_fingerprint_cells_source", "provider_fingerprint_cells", ["source_id", "run_id"])

    op.execute(
        """
        CREATE VIEW provider_fingerprint_latest AS
        WITH latest AS (
            SELECT id FROM provider_fingerprint_runs
            WHERE completed_at IS NOT NULL
            ORDER BY completed_at DESC,id DESC LIMIT 1
        )
        SELECT r.id AS run_id,r.model_version,r.code_sha,r.evidence_cutoff,
               r.minimum_forward_n,r.engineering_status,r.statistical_status AS run_statistical_status,
               r.provider_count,r.eligible_trade_count,r.cell_count,r.evidence_digest,
               c.source_id,COALESCE(s.chat_title,s.source_alias) AS provider_title,
               c.dimension_kind,c.side,c.session_bucket,c.sample_count,c.n_gate_met,
               c.primary_estimate_kind,c.posterior_json,c.descriptive_json,
               c.statistical_status,true AS research_only,false AS live_money_execution_allowed
        FROM latest l
        JOIN provider_fingerprint_runs r ON r.id=l.id
        LEFT JOIN provider_fingerprint_cells c ON c.run_id=r.id
        LEFT JOIN sources s ON s.id=c.source_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_fingerprint_latest")
    op.drop_index("ix_provider_fingerprint_cells_source", table_name="provider_fingerprint_cells")
    op.drop_table("provider_fingerprint_cells")
    op.drop_table("provider_fingerprint_runs")
