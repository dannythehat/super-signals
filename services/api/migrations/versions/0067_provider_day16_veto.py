"""Add immutable Day 16 AIDY veto/filter counterfactual evidence.

Revision ID: 0067_provider_day16_veto
Revises: 0066_shadow_enrollment_audit
Create Date: 2026-09-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0067_provider_day16_veto"
down_revision: str | None = "0066_shadow_enrollment_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_veto_counterfactual_decisions (
            id uuid PRIMARY KEY,
            signal_id uuid NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
            source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            signal_posted_at timestamptz NOT NULL,
            decided_at timestamptz NOT NULL,
            model_version varchar(64) NOT NULL,
            rule_version varchar(96) NOT NULL,
            research_action varchar(16) NOT NULL CHECK (research_action IN ('accept','reject')),
            reason varchar(160) NOT NULL,
            selected_hypothesis_id uuid REFERENCES provider_conditional_hypotheses(id),
            selected_shrunken_effect_r numeric,
            selected_cell_oos_n integer CHECK (selected_cell_oos_n IS NULL OR selected_cell_oos_n >= 0),
            selected_complement_oos_n integer CHECK (
                selected_complement_oos_n IS NULL OR selected_complement_oos_n >= 0
            ),
            input_digest varchar(64) NOT NULL,
            telegram_broadcast_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT telegram_broadcast_allowed),
            executable boolean NOT NULL DEFAULT false CHECK (NOT executable),
            statistical_authority varchar(64) NOT NULL
                DEFAULT 'WAITING-FOR-FORWARD-EVIDENCE'
                CHECK (statistical_authority='WAITING-FOR-FORWARD-EVIDENCE'),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(signal_id,rule_version),
            CHECK (decided_at >= signal_posted_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE provider_veto_counterfactual_outcomes (
            id uuid PRIMARY KEY,
            decision_id uuid NOT NULL UNIQUE
                REFERENCES provider_veto_counterfactual_decisions(id) ON DELETE CASCADE,
            signal_id uuid NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
            baseline_execution_adjusted_r numeric NOT NULL,
            filtered_execution_adjusted_r numeric NOT NULL,
            filter_delta_r numeric NOT NULL,
            avoided_loss_r numeric NOT NULL CHECK (avoided_loss_r >= 0),
            sacrificed_winner_r numeric NOT NULL CHECK (sacrificed_winner_r >= 0),
            resolved_at timestamptz NOT NULL,
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            statistical_authority varchar(64) NOT NULL
                DEFAULT 'WAITING-FOR-FORWARD-EVIDENCE'
                CHECK (statistical_authority='WAITING-FOR-FORWARD-EVIDENCE'),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_veto_decision_source_time "
        "ON provider_veto_counterfactual_decisions(source_id,signal_posted_at)"
    )
    op.execute(
        "CREATE INDEX ix_provider_veto_outcome_signal "
        "ON provider_veto_counterfactual_outcomes(signal_id,resolved_at)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION provider_day16_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Provider Day 16 evidence is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_veto_decision_append_only
        BEFORE UPDATE OR DELETE ON provider_veto_counterfactual_decisions
        FOR EACH ROW EXECUTE FUNCTION provider_day16_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_veto_outcome_append_only
        BEFORE UPDATE OR DELETE ON provider_veto_counterfactual_outcomes
        FOR EACH ROW EXECUTE FUNCTION provider_day16_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_veto_counterfactual_outcomes")
    op.execute("DROP TABLE IF EXISTS provider_veto_counterfactual_decisions")
    op.execute("DROP FUNCTION IF EXISTS provider_day16_append_only()")
