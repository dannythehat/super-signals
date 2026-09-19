"""Persist automatic forward acceptance checks for AIDY evidence grounding.

Revision ID: 0105_aidy_grounding_accept
Revises: 0104_aidy_grounding_health
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0105_aidy_grounding_accept"
down_revision: str | None = "0104_aidy_grounding_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE aidy_grounding_acceptance_runs (
            id uuid PRIMARY KEY,
            checked_at timestamptz NOT NULL,
            contract_version varchar(64) NOT NULL,
            state varchar(32) NOT NULL,
            total_rows integer NOT NULL,
            clean_rows integer NOT NULL,
            invalid_rows integer NOT NULL,
            rows_with_provider_claims integer NOT NULL,
            distinct_providers integer NOT NULL,
            required_rows integer NOT NULL,
            required_providers integer NOT NULL,
            failure_details jsonb NOT NULL DEFAULT '[]'::jsonb,
            research_only boolean NOT NULL DEFAULT true,
            live_money_execution_allowed boolean NOT NULL DEFAULT false,
            CONSTRAINT ck_aidy_grounding_acceptance_state
                CHECK (state IN ('waiting_forward_rows','clean_so_far','accepted','failed')),
            CONSTRAINT ck_aidy_grounding_acceptance_counts
                CHECK (
                    total_rows >= 0
                    AND clean_rows >= 0
                    AND invalid_rows >= 0
                    AND rows_with_provider_claims >= 0
                    AND distinct_providers >= 0
                    AND required_rows > 0
                    AND required_providers > 0
                    AND clean_rows + invalid_rows = total_rows
                ),
            CONSTRAINT ck_aidy_grounding_acceptance_failure_details
                CHECK (jsonb_typeof(failure_details)='array'),
            CONSTRAINT ck_aidy_grounding_acceptance_research_only
                CHECK (research_only = true),
            CONSTRAINT ck_aidy_grounding_acceptance_no_live_money
                CHECK (live_money_execution_allowed = false)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_aidy_grounding_acceptance_checked
        ON aidy_grounding_acceptance_runs(checked_at DESC)
        """
    )
    op.execute(
        """
        CREATE VIEW aidy_grounding_acceptance_latest AS
        SELECT *
        FROM aidy_grounding_acceptance_runs
        ORDER BY checked_at DESC
        LIMIT 1
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS aidy_grounding_acceptance_latest")
    op.execute("DROP INDEX IF EXISTS ix_aidy_grounding_acceptance_checked")
    op.execute("DROP TABLE IF EXISTS aidy_grounding_acceptance_runs")


