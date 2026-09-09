"""Add immutable Provider Intelligence Day 21 full-chain evidence.

Revision ID: 0070_provider_day21_full_chain
Revises: 0069_provider_day20_management
Create Date: 2026-09-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0070_provider_day21_full_chain"
down_revision: str | None = "0069_provider_day20_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_day21_signal_chains (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            signal_id uuid NOT NULL UNIQUE REFERENCES signals(id) ON DELETE CASCADE,
            source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            signal_posted_at timestamptz NOT NULL,
            anchored_at timestamptz NOT NULL DEFAULT now(),
            anchor_delay_seconds numeric NOT NULL CHECK (anchor_delay_seconds >= 0),
            forward_decision_eligible boolean NOT NULL,
            contract_version varchar(64) NOT NULL DEFAULT 'provider_day21_chain_v1'
                CHECK (contract_version='provider_day21_chain_v1'),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            paper_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT paper_execution_allowed),
            live_variable_sizing_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_variable_sizing_allowed),
            paper_variable_sizing_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT paper_variable_sizing_allowed),
            live_management_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_management_allowed),
            paper_management_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT paper_management_allowed),
            public_broadcast_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT public_broadcast_allowed)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_day21_chain_source_time "
        "ON provider_day21_signal_chains(source_id,signal_posted_at)"
    )

    op.execute(
        """
        CREATE TABLE provider_day21_chain_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            chain_id uuid NOT NULL REFERENCES provider_day21_signal_chains(id) ON DELETE CASCADE,
            signal_id uuid NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
            stage varchar(40) NOT NULL CHECK (stage IN (
                'enrollment','provider_profile','aidy_context','day16_veto',
                'day17_sizing','day18_book','day19_explanation','day20_management',
                'outcome_feedback'
            )),
            event_key varchar(160) NOT NULL,
            status varchar(80) NOT NULL,
            observed_at timestamptz NOT NULL DEFAULT now(),
            evidence_as_of timestamptz,
            evidence_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            evidence_digest varchar(64) NOT NULL,
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            paper_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT paper_execution_allowed),
            live_variable_sizing_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_variable_sizing_allowed),
            paper_variable_sizing_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT paper_variable_sizing_allowed),
            live_management_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_management_allowed),
            paper_management_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT paper_management_allowed),
            public_broadcast_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT public_broadcast_allowed),
            UNIQUE(signal_id,stage,event_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_day21_events_chain_stage "
        "ON provider_day21_chain_events(chain_id,stage,observed_at)"
    )

    op.execute(
        """
        CREATE VIEW provider_day21_chain_state AS
        SELECT c.id AS chain_id,c.signal_id,c.source_id,c.message_id,c.signal_posted_at,
               c.anchored_at,c.anchor_delay_seconds,c.forward_decision_eligible,
               bool_or(e.stage='enrollment') AS enrollment_recorded,
               bool_or(e.stage='provider_profile') AS provider_profile_recorded,
               bool_or(e.stage='aidy_context') AS aidy_context_recorded,
               bool_or(e.stage='day16_veto') AS day16_recorded,
               bool_or(e.stage='day17_sizing') AS day17_recorded,
               bool_or(e.stage='day18_book') AS day18_recorded,
               bool_or(e.stage='day19_explanation') AS day19_recorded,
               bool_or(e.stage='day20_management') AS day20_recorded,
               bool_or(e.stage='outcome_feedback') AS outcome_feedback_recorded,
               count(e.id) AS event_count,
               true AS research_only,false AS live_money_execution_allowed,
               false AS paper_execution_allowed,false AS live_variable_sizing_allowed,
               false AS paper_variable_sizing_allowed,false AS live_management_allowed,
               false AS paper_management_allowed,false AS public_broadcast_allowed
        FROM provider_day21_signal_chains c
        LEFT JOIN provider_day21_chain_events e ON e.chain_id=c.id
        GROUP BY c.id
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION provider_day21_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Provider Day 21 chain evidence is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_day21_chain_append_only
        BEFORE UPDATE OR DELETE ON provider_day21_signal_chains
        FOR EACH ROW EXECUTE FUNCTION provider_day21_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_day21_event_append_only
        BEFORE UPDATE OR DELETE ON provider_day21_chain_events
        FOR EACH ROW EXECUTE FUNCTION provider_day21_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_day21_chain_state")
    op.execute("DROP TABLE IF EXISTS provider_day21_chain_events")
    op.execute("DROP TABLE IF EXISTS provider_day21_signal_chains")
    op.execute("DROP FUNCTION IF EXISTS provider_day21_append_only()")
