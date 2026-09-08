"""Add immutable Day 20 active-management counterfactual evidence.

Revision ID: 0069_provider_day20_mgmt
Revises: 0068_provider_day19_usage
Create Date: 2026-09-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0069_provider_day20_mgmt"
down_revision: str | None = "0068_provider_day19_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_management_counterfactual_decisions (
            id uuid PRIMARY KEY,
            signal_id uuid NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
            source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            decided_at timestamptz NOT NULL,
            model_version varchar(64) NOT NULL,
            action varchar(24) NOT NULL CHECK (action IN ('hold','protect','reduce','early_close')),
            reason varchar(180) NOT NULL,
            reduce_fraction numeric,
            protected_stop_r numeric,
            decision_digest varchar(64) NOT NULL,
            executable boolean NOT NULL DEFAULT false CHECK (NOT executable),
            management_efficacy varchar(64) NOT NULL
                DEFAULT 'WAITING-FOR-FORWARD-EVIDENCE'
                CHECK (management_efficacy='WAITING-FOR-FORWARD-EVIDENCE'),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(signal_id,model_version,decided_at),
            CHECK (reduce_fraction IS NULL OR (reduce_fraction > 0 AND reduce_fraction < 1))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE provider_management_counterfactual_outcomes (
            id uuid PRIMARY KEY,
            decision_id uuid NOT NULL UNIQUE
                REFERENCES provider_management_counterfactual_decisions(id) ON DELETE CASCADE,
            signal_id uuid NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
            untouched_provider_baseline_r numeric NOT NULL,
            intervention_gross_r numeric NOT NULL,
            intervention_execution_cost_r numeric NOT NULL CHECK (intervention_execution_cost_r >= 0),
            intervention_net_r numeric NOT NULL,
            management_delta_r numeric NOT NULL,
            loss_saved_r numeric NOT NULL CHECK (loss_saved_r >= 0),
            winner_sacrificed_r numeric NOT NULL CHECK (winner_sacrificed_r >= 0),
            resolved_at timestamptz NOT NULL,
            management_efficacy varchar(64) NOT NULL
                DEFAULT 'WAITING-FOR-FORWARD-EVIDENCE'
                CHECK (management_efficacy='WAITING-FOR-FORWARD-EVIDENCE'),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE provider_aidy_authority_audit (
            id uuid PRIMARY KEY,
            observed_at timestamptz NOT NULL,
            global_aidy_revoked boolean NOT NULL,
            kill_switch_active boolean NOT NULL,
            stale_inputs boolean NOT NULL,
            daily_loss_limit_breached boolean NOT NULL,
            max_drawdown_limit_breached boolean NOT NULL,
            consecutive_loss_limit_breached boolean NOT NULL,
            paper_live_divergence boolean NOT NULL,
            auto_revert_to_provider_baseline boolean NOT NULL,
            live_management_allowed boolean NOT NULL DEFAULT false CHECK (NOT live_management_allowed),
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_management_signal_time "
        "ON provider_management_counterfactual_decisions(signal_id,decided_at)"
    )
    op.execute(
        "CREATE INDEX ix_provider_aidy_authority_time "
        "ON provider_aidy_authority_audit(observed_at)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION provider_day20_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Provider Day 20 evidence is append-only';
        END;
        $$
        """
    )
    for table in (
        "provider_management_counterfactual_decisions",
        "provider_management_counterfactual_outcomes",
        "provider_aidy_authority_audit",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION provider_day20_append_only()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_aidy_authority_audit")
    op.execute("DROP TABLE IF EXISTS provider_management_counterfactual_outcomes")
    op.execute("DROP TABLE IF EXISTS provider_management_counterfactual_decisions")
    op.execute("DROP FUNCTION IF EXISTS provider_day20_append_only()")
