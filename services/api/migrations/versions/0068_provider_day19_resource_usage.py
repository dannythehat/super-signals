"""Add immutable Provider Intelligence resource/compliance budget observations.

Revision ID: 0068_provider_day19_usage
Revises: 0067_provider_day16_veto
Create Date: 2026-09-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0068_provider_day19_usage"
down_revision: str | None = "0067_provider_day16_veto"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_intelligence_resource_usage (
            id uuid PRIMARY KEY,
            observed_at timestamptz NOT NULL,
            window_key varchar(64) NOT NULL,
            model_version varchar(64) NOT NULL,
            d1_reads bigint NOT NULL CHECK (d1_reads >= 0),
            metaapi_calls bigint NOT NULL CHECK (metaapi_calls >= 0),
            openai_calls bigint NOT NULL CHECK (openai_calls >= 0),
            openai_input_tokens bigint NOT NULL CHECK (openai_input_tokens >= 0),
            openai_output_tokens bigint NOT NULL CHECK (openai_output_tokens >= 0),
            estimated_cost_usd numeric NOT NULL CHECK (estimated_cost_usd >= 0),
            budget_status varchar(24) NOT NULL CHECK (
                budget_status IN ('WITHIN_BUDGET','SOFT_ALERT','HARD_LIMIT')
            ),
            research_enrichment_allowed boolean NOT NULL,
            live_execution_affected boolean NOT NULL DEFAULT false
                CHECK (NOT live_execution_affected),
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(window_key,observed_at)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_intelligence_resource_usage_time "
        "ON provider_intelligence_resource_usage(observed_at,budget_status)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION provider_day19_usage_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Provider Day 19 resource evidence is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_day19_usage_append_only
        BEFORE UPDATE OR DELETE ON provider_intelligence_resource_usage
        FOR EACH ROW EXECUTE FUNCTION provider_day19_usage_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_intelligence_resource_usage")
    op.execute("DROP FUNCTION IF EXISTS provider_day19_usage_append_only()")
