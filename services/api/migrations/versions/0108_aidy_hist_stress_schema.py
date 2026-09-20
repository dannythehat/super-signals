"""Allow reconstructed AIDY historical stress-lab partitions and evidence.

The original replay schema was intentionally narrow for the exact-PIT exam. The
separate reconstructed stress lab uses different research-only partition labels,
evidence tier, and run scopes. This migration expands only those CHECK contracts
and the partition column widths; it does not change execution authority.

Revision ID: 0108_aidy_hist_stress_schema
Revises: 0107_aidy_hist_replay_v2
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0108_aidy_hist_stress_schema"
down_revision: str | None = "0107_aidy_hist_replay_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _recreate_scoreboard_view() -> None:
    op.execute(
        """
        CREATE VIEW aidy_historical_replay_scoreboard AS
        SELECT
            c.partition,
            d.replay_version,
            d.model_version,
            d.prompt_version,
            count(*) AS scored,
            round(sum(s.actual_pnl_usd), 2) AS taken_pnl_usd,
            round(sum(s.replay_shadow_pnl_usd), 2) AS replay_shadow_pnl_usd,
            round(sum(s.replay_delta_vs_taken_usd), 2) AS replay_delta_vs_taken_usd,
            round(avg(s.replay_delta_vs_taken_usd), 4) AS avg_delta_usd,
            count(*) FILTER (WHERE s.replay_delta_vs_taken_usd > 0) AS improved,
            count(*) FILTER (WHERE s.replay_delta_vs_taken_usd < 0) AS harmed,
            count(*) FILTER (WHERE s.replay_delta_vs_taken_usd = 0) AS unchanged
        FROM aidy_historical_replay_scores s
        JOIN aidy_historical_replay_decisions d ON d.id=s.replay_decision_id
        JOIN aidy_historical_replay_cases c ON c.id=d.case_id
        GROUP BY c.partition,d.replay_version,d.model_version,d.prompt_version
        """
    )


def upgrade() -> None:
    # PostgreSQL will not alter a column type while a view depends on it.
    # Drop/recreate only the research scoreboard view inside this transactional migration.
    op.execute("DROP VIEW IF EXISTS aidy_historical_replay_scoreboard")
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        DROP CONSTRAINT IF EXISTS ck_aidy_hist_case_partition
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        ALTER COLUMN partition TYPE varchar(32)
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        ADD CONSTRAINT ck_aidy_hist_case_partition
        CHECK (
            partition IN (
                'development','validation','holdout',
                'research_train','research_validation','research_oos'
            )
        )
        """
    )

    op.execute(
        """
        ALTER TABLE aidy_historical_replay_scores
        DROP CONSTRAINT IF EXISTS ck_aidy_hist_score_partition
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_scores
        ALTER COLUMN partition TYPE varchar(32)
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_scores
        ADD CONSTRAINT ck_aidy_hist_score_partition
        CHECK (
            partition IN (
                'development','validation','holdout',
                'research_train','research_validation','research_oos'
            )
        )
        """
    )

    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        DROP CONSTRAINT IF EXISTS ck_aidy_hist_case_tier
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        ADD CONSTRAINT ck_aidy_hist_case_tier
        CHECK (
            evidence_tier IN (
                'exact_pit','provider_only','geometry_only','reconstructed_research'
            )
        )
        """
    )

    op.execute(
        """
        ALTER TABLE aidy_historical_replay_runs
        DROP CONSTRAINT IF EXISTS ck_aidy_hist_run_scope
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_runs
        ADD CONSTRAINT ck_aidy_hist_run_scope
        CHECK (
            partition_scope IN (
                'development','development_validation','holdout',
                'train','validation','train_validation','oos','all'
            )
        )
        """
    )
    _recreate_scoreboard_view()


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS aidy_historical_replay_scoreboard")
    # A downgrade is only valid after reconstructed stress rows have been removed;
    # restoring the original narrow constraints intentionally fails otherwise.
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_runs
        DROP CONSTRAINT IF EXISTS ck_aidy_hist_run_scope
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_runs
        ADD CONSTRAINT ck_aidy_hist_run_scope
        CHECK (partition_scope IN ('development','development_validation','holdout'))
        """
    )

    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        DROP CONSTRAINT IF EXISTS ck_aidy_hist_case_tier
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        ADD CONSTRAINT ck_aidy_hist_case_tier
        CHECK (evidence_tier IN ('exact_pit','provider_only','geometry_only'))
        """
    )

    op.execute(
        """
        ALTER TABLE aidy_historical_replay_scores
        DROP CONSTRAINT IF EXISTS ck_aidy_hist_score_partition
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_scores
        ALTER COLUMN partition TYPE varchar(16)
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_scores
        ADD CONSTRAINT ck_aidy_hist_score_partition
        CHECK (partition IN ('development','validation','holdout'))
        """
    )

    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        DROP CONSTRAINT IF EXISTS ck_aidy_hist_case_partition
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        ALTER COLUMN partition TYPE varchar(16)
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        ADD CONSTRAINT ck_aidy_hist_case_partition
        CHECK (partition IN ('development','validation','holdout'))
        """
    )
    _recreate_scoreboard_view()
