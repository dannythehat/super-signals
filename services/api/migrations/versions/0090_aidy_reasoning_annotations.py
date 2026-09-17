"""Give AIDY an actual reasoning call per decision it could not resolve from track record alone.

The Decision Ledger (0088) is deliberately deterministic: duplicate/conflict checks and a
provider's own recorded track record. Its own docstring says why: "AIDY to weigh market
conditions, news, candles and forecasts -- that reasoning needs a real model call per
signal, which is a real ongoing cost, and it is not switched on silently." A provider with
fewer than 20 resolved trades gets `approve` + `insufficient_track_record_evidence` today --
correct, but AIDY has nothing to say about *that specific signal*. This adds one thing: an
LLM reads the signal, the provider's thin history and the deterministic reasoning already
computed, and writes a rationale, a qualitative lean and a confidence figure -- additive and
observational only. It never replaces or overrides `aidy_decisions.decision_class`, and it
earns no more authority than the deterministic engine already has: research_only and
live_money_execution_allowed are hard-constrained exactly like every other AIDY table.

Revision ID: 0090_aidy_reasoning_annotations
Revises: 0089_provider_scoreboard_cohorts
Create Date: 2026-09-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0090_aidy_reasoning_annotations"
down_revision: str | None = "0089_provider_scoreboard_cohorts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE aidy_reasoning_annotations (
            id uuid PRIMARY KEY,
            decision_id uuid NOT NULL UNIQUE
                REFERENCES aidy_decisions(id) ON DELETE CASCADE,
            -- Never invented: a model call either returns one of these three or the row
            -- is not written at all (a failed/ambiguous call is retried later, not
            -- persisted as a guess).
            lean varchar(16) NOT NULL CHECK (lean IN ('agree','caution','disagree')),
            confidence numeric NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            rationale text NOT NULL,
            key_factors jsonb NOT NULL,
            model_version varchar(64) NOT NULL,
            prompt_version varchar(64) NOT NULL,
            model_name varchar(64) NOT NULL,
            response_id varchar(128),
            input_tokens integer NOT NULL CHECK (input_tokens >= 0),
            output_tokens integer NOT NULL CHECK (output_tokens >= 0),
            estimated_cost_usd numeric NOT NULL CHECK (estimated_cost_usd >= 0),
            latency_ms integer NOT NULL CHECK (latency_ms >= 0),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_aidy_reasoning_annotations_created "
        "ON aidy_reasoning_annotations(created_at)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aidy_reasoning_annotations_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'AIDY reasoning annotations are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_aidy_reasoning_annotations_append_only
        BEFORE UPDATE OR DELETE ON aidy_reasoning_annotations
        FOR EACH ROW EXECUTE FUNCTION aidy_reasoning_annotations_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS aidy_reasoning_annotations")
    op.execute("DROP FUNCTION IF EXISTS aidy_reasoning_annotations_append_only()")
