"""Add immutable historical replay exam ledgers for AIDY.

The replay system is research-only and separated from production reasoning/execution.
Inputs are frozen before any outcome row is joined. Holdout rows are partitioned at
materialization time and are locked unless an explicit research flag is enabled.

Revision ID: 0106_aidy_hist_replay
Revises: 0105_aidy_grounding_accept
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0106_aidy_hist_replay"
down_revision: str | None = "0105_aidy_grounding_accept"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE aidy_historical_replay_cases (
            id uuid PRIMARY KEY,
            source_decision_id uuid NOT NULL UNIQUE REFERENCES aidy_decisions(id),
            source_id uuid NOT NULL REFERENCES sources(id),
            signal_posted_at timestamptz NOT NULL,
            partition varchar(16) NOT NULL,
            evidence_tier varchar(32) NOT NULL,
            input_contract_version varchar(64) NOT NULL,
            input_payload jsonb NOT NULL,
            input_digest varchar(64) NOT NULL,
            model_eligible boolean NOT NULL,
            research_only boolean NOT NULL DEFAULT true,
            live_money_execution_allowed boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_aidy_hist_case_partition
                CHECK (partition IN ('development','validation','holdout')),
            CONSTRAINT ck_aidy_hist_case_tier
                CHECK (evidence_tier IN ('exact_pit','provider_only','geometry_only')),
            CONSTRAINT ck_aidy_hist_case_payload_object
                CHECK (jsonb_typeof(input_payload)='object'),
            CONSTRAINT ck_aidy_hist_case_research_only CHECK (research_only=true),
            CONSTRAINT ck_aidy_hist_case_no_live CHECK (live_money_execution_allowed=false)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_aidy_hist_case_partition_time
        ON aidy_historical_replay_cases(partition, signal_posted_at, id)
        """
    )

    op.execute(
        """
        CREATE TABLE aidy_historical_replay_decisions (
            id uuid PRIMARY KEY,
            case_id uuid NOT NULL UNIQUE REFERENCES aidy_historical_replay_cases(id),
            replay_version varchar(64) NOT NULL,
            model_version varchar(64) NOT NULL,
            prompt_version varchar(64) NOT NULL,
            model_name varchar(128) NOT NULL,
            input_digest varchar(64) NOT NULL,
            output_payload jsonb NOT NULL,
            response_id varchar(255),
            input_tokens integer NOT NULL,
            output_tokens integer NOT NULL,
            estimated_cost_usd numeric NOT NULL,
            latency_ms integer NOT NULL,
            research_only boolean NOT NULL DEFAULT true,
            live_money_execution_allowed boolean NOT NULL DEFAULT false,
            decided_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_aidy_hist_decision_payload_object
                CHECK (jsonb_typeof(output_payload)='object'),
            CONSTRAINT ck_aidy_hist_decision_research_only CHECK (research_only=true),
            CONSTRAINT ck_aidy_hist_decision_no_live CHECK (live_money_execution_allowed=false)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE aidy_historical_replay_scores (
            id uuid PRIMARY KEY,
            replay_decision_id uuid NOT NULL UNIQUE
                REFERENCES aidy_historical_replay_decisions(id),
            source_decision_id uuid NOT NULL REFERENCES aidy_decisions(id),
            partition varchar(16) NOT NULL,
            resolution varchar(64) NOT NULL,
            baseline_pnl_usd numeric NOT NULL,
            baseline_realized_r numeric NOT NULL,
            actual_pnl_usd numeric NOT NULL,
            actual_realized_r numeric NOT NULL,
            replay_shadow_pnl_usd numeric NOT NULL,
            replay_delta_vs_taken_usd numeric NOT NULL,
            outcome_resolved_at timestamptz NOT NULL,
            scored_at timestamptz NOT NULL DEFAULT now(),
            research_only boolean NOT NULL DEFAULT true,
            live_money_execution_allowed boolean NOT NULL DEFAULT false,
            CONSTRAINT ck_aidy_hist_score_partition
                CHECK (partition IN ('development','validation','holdout')),
            CONSTRAINT ck_aidy_hist_score_research_only CHECK (research_only=true),
            CONSTRAINT ck_aidy_hist_score_no_live CHECK (live_money_execution_allowed=false)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE aidy_historical_replay_runs (
            id uuid PRIMARY KEY,
            replay_version varchar(64) NOT NULL,
            model_version varchar(64) NOT NULL,
            prompt_version varchar(64) NOT NULL,
            partition_scope varchar(32) NOT NULL,
            cases_materialized integer NOT NULL,
            decisions_written integer NOT NULL,
            decisions_failed integer NOT NULL,
            scores_written integer NOT NULL,
            total_replay_delta_usd numeric NOT NULL,
            total_replay_shadow_pnl_usd numeric NOT NULL,
            holdout_opened boolean NOT NULL,
            research_only boolean NOT NULL DEFAULT true,
            live_money_execution_allowed boolean NOT NULL DEFAULT false,
            started_at timestamptz NOT NULL,
            finished_at timestamptz NOT NULL,
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT ck_aidy_hist_run_scope
                CHECK (partition_scope IN ('development','development_validation','holdout')),
            CONSTRAINT ck_aidy_hist_run_counts
                CHECK (
                    cases_materialized >= 0
                    AND decisions_written >= 0
                    AND decisions_failed >= 0
                    AND scores_written >= 0
                ),
            CONSTRAINT ck_aidy_hist_run_details_object CHECK (jsonb_typeof(details)='object'),
            CONSTRAINT ck_aidy_hist_run_research_only CHECK (research_only=true),
            CONSTRAINT ck_aidy_hist_run_no_live CHECK (live_money_execution_allowed=false)
        )
        """
    )

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


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS aidy_historical_replay_scoreboard")
    op.execute("DROP TABLE IF EXISTS aidy_historical_replay_runs")
    op.execute("DROP TABLE IF EXISTS aidy_historical_replay_scores")
    op.execute("DROP TABLE IF EXISTS aidy_historical_replay_decisions")
    op.execute("DROP INDEX IF EXISTS ix_aidy_hist_case_partition_time")
    op.execute("DROP TABLE IF EXISTS aidy_historical_replay_cases")
