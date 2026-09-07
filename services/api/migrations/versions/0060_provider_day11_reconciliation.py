"""Repair Provider Lab lifecycle storage and add Day 11 reconciliation evidence.

Revision ID: 0060_provider_day11_reconciliation
Revises: 0059_provider_exec_calibration
Create Date: 2026-09-07

All new surfaces are research-only. They cannot place trades, change member sizing,
change provider production status, or grant AIDY live-money authority.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060_provider_day11_reconciliation"
down_revision: str | None = "0059_provider_exec_calibration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A legitimate fail-closed reason emitted by shadow_trading_v3 is 46 characters.
    # VARCHAR(40) could therefore reject truthful lifecycle evidence.
    op.alter_column(
        "shadow_trade_legs",
        "exit_reason",
        existing_type=sa.String(length=40),
        type_=sa.String(length=120),
        existing_nullable=True,
    )

    op.create_table(
        "provider_execution_reconciliation_tolerances",
        sa.Column("version", sa.String(length=64), primary_key=True),
        sa.Column("min_provider_signals", sa.Integer(), nullable=False),
        sa.Column("min_total_signals", sa.Integer(), nullable=False),
        sa.Column("max_median_abs_r_delta", sa.Numeric(10, 6), nullable=False),
        sa.Column("max_p95_abs_r_delta", sa.Numeric(10, 6), nullable=False),
        sa.Column("min_lifecycle_agreement_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("min_provider_signals >= 1", name="ck_provider_recon_tol_provider_n"),
        sa.CheckConstraint("min_total_signals >= min_provider_signals", name="ck_provider_recon_tol_total_n"),
        sa.CheckConstraint("max_median_abs_r_delta >= 0", name="ck_provider_recon_tol_median_r"),
        sa.CheckConstraint("max_p95_abs_r_delta >= max_median_abs_r_delta", name="ck_provider_recon_tol_p95_r"),
        sa.CheckConstraint(
            "min_lifecycle_agreement_rate >= 0 AND min_lifecycle_agreement_rate <= 1",
            name="ck_provider_recon_tol_lifecycle",
        ),
        sa.CheckConstraint("research_only", name="ck_provider_recon_tol_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_recon_tol_no_live_money"),
    )

    op.execute(
        """
        INSERT INTO provider_execution_reconciliation_tolerances(
            version,min_provider_signals,min_total_signals,max_median_abs_r_delta,
            max_p95_abs_r_delta,min_lifecycle_agreement_rate,note
        ) VALUES (
            'provider_day11_v1',5,30,0.35,1.00,0.80,
            'Engineering paper-to-broker calibration only; not a trading-edge or promotion threshold.'
        )
        ON CONFLICT (version) DO NOTHING
        """
    )

    op.create_table(
        "provider_execution_reconciliation_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tolerance_version",
            sa.String(length=64),
            sa.ForeignKey("provider_execution_reconciliation_tolerances.version"),
            nullable=False,
        ),
        sa.Column("code_sha", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=80), nullable=False, server_default="RUNNING"),
        sa.Column("provider_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_attempted_signals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_comparable_signals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("provider_count >= 0", name="ck_provider_recon_run_provider_count"),
        sa.CheckConstraint("total_attempted_signals >= 0", name="ck_provider_recon_run_attempted"),
        sa.CheckConstraint("total_comparable_signals >= 0", name="ck_provider_recon_run_comparable"),
        sa.CheckConstraint("research_only", name="ck_provider_recon_run_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_recon_run_no_live_money"),
    )
    op.create_index(
        "ix_provider_recon_runs_completed",
        "provider_execution_reconciliation_runs",
        ["completed_at"],
    )

    op.create_table(
        "provider_execution_reconciliation_samples",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_execution_reconciliation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("signals.id"), nullable=False),
        sa.Column("signal_posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paper_r", sa.Numeric(18, 8), nullable=True),
        sa.Column("broker_r", sa.Numeric(18, 8), nullable=True),
        sa.Column("abs_r_delta", sa.Numeric(18, 8), nullable=True),
        sa.Column("paper_lifecycle", sa.String(length=64), nullable=True),
        sa.Column("broker_lifecycle", sa.String(length=64), nullable=True),
        sa.Column("lifecycle_matches", sa.Boolean(), nullable=True),
        sa.Column("replay_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("replay_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aidy_evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("broker_deal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exclusion_reason", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("run_id", "signal_id", name="uq_provider_recon_run_signal"),
        sa.CheckConstraint("broker_deal_count >= 0", name="ck_provider_recon_sample_deals"),
        sa.CheckConstraint("replay_to > replay_from", name="ck_provider_recon_sample_window"),
        sa.CheckConstraint("abs_r_delta IS NULL OR abs_r_delta >= 0", name="ck_provider_recon_sample_abs_r"),
        sa.CheckConstraint("research_only", name="ck_provider_recon_sample_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_recon_sample_no_live_money"),
    )
    op.create_index(
        "ix_provider_recon_samples_source_run",
        "provider_execution_reconciliation_samples",
        ["source_id", "run_id"],
    )

    op.create_table(
        "provider_execution_reconciliation_provider_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_execution_reconciliation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("median_abs_r_delta", sa.Numeric(18, 8), nullable=False),
        sa.Column("p95_abs_r_delta", sa.Numeric(18, 8), nullable=False),
        sa.Column("lifecycle_agreement_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("status", sa.String(length=80), nullable=False),
        sa.Column("intelligence_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_money_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("run_id", "source_id", name="uq_provider_recon_result_run_source"),
        sa.CheckConstraint("sample_count >= 0", name="ck_provider_recon_result_sample_count"),
        sa.CheckConstraint("median_abs_r_delta >= 0", name="ck_provider_recon_result_median"),
        sa.CheckConstraint("p95_abs_r_delta >= median_abs_r_delta", name="ck_provider_recon_result_p95"),
        sa.CheckConstraint(
            "lifecycle_agreement_rate >= 0 AND lifecycle_agreement_rate <= 1",
            name="ck_provider_recon_result_lifecycle",
        ),
        sa.CheckConstraint(
            "intelligence_mode IN ('RESEARCH_READY','SHADOW_WAITING')",
            name="ck_provider_recon_result_mode",
        ),
        sa.CheckConstraint("research_only", name="ck_provider_recon_result_research_only"),
        sa.CheckConstraint("NOT live_money_execution_allowed", name="ck_provider_recon_result_no_live_money"),
    )

    op.execute(
        """
        CREATE VIEW provider_execution_reconciliation_latest AS
        WITH latest AS (
            SELECT id
            FROM provider_execution_reconciliation_runs
            WHERE completed_at IS NOT NULL
            ORDER BY completed_at DESC,id DESC
            LIMIT 1
        )
        SELECT
            r.id AS run_id,
            r.tolerance_version,
            r.code_sha,
            r.started_at,
            r.completed_at,
            r.status AS run_status,
            r.provider_count,
            r.total_attempted_signals,
            r.total_comparable_signals,
            r.evidence_digest,
            pr.source_id,
            COALESCE(s.chat_title,s.source_alias) AS provider_title,
            pr.sample_count,
            pr.median_abs_r_delta,
            pr.p95_abs_r_delta,
            pr.lifecycle_agreement_rate,
            pr.status AS provider_status,
            pr.intelligence_mode,
            true AS research_only,
            false AS live_money_execution_allowed
        FROM latest l
        JOIN provider_execution_reconciliation_runs r ON r.id=l.id
        LEFT JOIN provider_execution_reconciliation_provider_results pr ON pr.run_id=r.id
        LEFT JOIN sources s ON s.id=pr.source_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_execution_reconciliation_latest")
    op.drop_table("provider_execution_reconciliation_provider_results")
    op.drop_index("ix_provider_recon_samples_source_run", table_name="provider_execution_reconciliation_samples")
    op.drop_table("provider_execution_reconciliation_samples")
    op.drop_index("ix_provider_recon_runs_completed", table_name="provider_execution_reconciliation_runs")
    op.drop_table("provider_execution_reconciliation_runs")
    op.drop_table("provider_execution_reconciliation_tolerances")
    # Intentionally retain VARCHAR(120) for shadow_trade_legs.exit_reason.
    # Day 10 legitimately emits `entry_stop_sequence_ambiguous_same_observation`
    # (46 characters). Narrowing back to VARCHAR(40) is destructive and already
    # fails the migration cycle when truthful lifecycle evidence is present.
    # A Day 11 feature downgrade therefore removes only Day 11 feature surfaces;
    # the independent data-integrity repair survives the downgrade.
