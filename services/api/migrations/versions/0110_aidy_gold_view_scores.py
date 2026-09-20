"""Score AIDY's frozen independent Gold view separately from provider-trade P&L.

A provider action score answers whether take/reduce/reject improved the provider trade.
This table answers a different question: when AIDY independently said Gold was bullish
or bearish before the outcome, did price actually move that way over AIDY's frozen
horizon? It is a research directional probe, not broker P&L and not execution authority.

Revision ID: 0110_aidy_gold_view_scores
Revises: 0109_aidy_gold_view
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0110_aidy_gold_view_scores"
down_revision: str | None = "0109_aidy_gold_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE aidy_historical_gold_view_scores (
            id uuid PRIMARY KEY,
            replay_decision_id uuid NOT NULL UNIQUE
                REFERENCES aidy_historical_replay_decisions(id) ON DELETE CASCADE,
            source_decision_id uuid NOT NULL REFERENCES aidy_decisions(id) ON DELETE CASCADE,
            partition varchar(32) NOT NULL CHECK (
                partition IN (
                    'development','validation','holdout',
                    'research_train','research_validation','research_oos'
                )
            ),
            signal_posted_at timestamptz NOT NULL,
            provider_side varchar(8) CHECK (provider_side IN ('BUY','SELL')),
            provider_alignment varchar(16) NOT NULL CHECK (
                provider_alignment IN ('aligned','conflicts','unclear')
            ),
            gold_view_direction varchar(16) NOT NULL CHECK (
                gold_view_direction IN ('bullish','bearish','neutral','unknown')
            ),
            gold_view_confidence numeric NOT NULL CHECK (
                gold_view_confidence >= 0 AND gold_view_confidence <= 1
            ),
            horizon_minutes integer NOT NULL CHECK (
                horizon_minutes >= 0 AND horizon_minutes <= 240
            ),
            reference_time_utc timestamptz,
            reference_price numeric,
            terminal_time_utc timestamptz,
            terminal_price numeric,
            directional_move_points numeric,
            favorable_excursion_points numeric,
            adverse_excursion_points numeric,
            outcome_class varchar(32) NOT NULL CHECK (
                outcome_class IN (
                    'favorable','adverse','flat',
                    'abstain_neutral','abstain_unknown',
                    'insufficient_market_path'
                )
            ),
            market_path_complete boolean NOT NULL DEFAULT false,
            evidence_tier varchar(32) NOT NULL DEFAULT 'retrospective_research',
            research_only boolean NOT NULL DEFAULT true CHECK (research_only=true),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (live_money_execution_allowed=false),
            scored_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_aidy_hist_gold_view_scoreboard
        ON aidy_historical_gold_view_scores(
            partition,gold_view_direction,provider_alignment,outcome_class
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aidy_historical_gold_view_scores_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'AIDY historical Gold-view scores are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_aidy_historical_gold_view_scores_append_only
        BEFORE UPDATE OR DELETE ON aidy_historical_gold_view_scores
        FOR EACH ROW EXECUTE FUNCTION aidy_historical_gold_view_scores_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS aidy_historical_gold_view_scores")
    op.execute(
        "DROP FUNCTION IF EXISTS aidy_historical_gold_view_scores_append_only()"
    )
