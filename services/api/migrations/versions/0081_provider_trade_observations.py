"""Record every provider trade the interpreter understood, executable or not.

Until now a provider trade only became visible to research when it was also
executable. ``ai_message_pipeline_canonical`` creates a Signal on
``action == "execute"`` and otherwise stores the decision and stops, so a trade the
model read correctly but could not be mirrored left no research trace at all.

Measured on production 2026-09-14: 2,206 new trades were identified and skipped, and
6,637 trade updates were identified and ignored. Whole groups were invisible for five
weeks -- The Gold Club posted 508 understood trades and produced zero signals, because
it does not publish a stop loss and the execution gate is also the research gate.

"This group never posts a stop loss" is a finding about the group. It belongs in the
scoreboard, not in the bin. This table is that record: one row per understood trade or
update, with the reason it was or was not executable.

It is research evidence only. ``research_only`` and ``live_execution_affected`` are
CHECK-constrained so no future writer can quietly turn this into an execution input,
and the table is append-only so the observed history cannot be rewritten.

Revision ID: 0081_provider_trade_observations
Revises: 0080_shadow_tig_asia
Create Date: 2026-09-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0081_provider_trade_observations"
down_revision: str | None = "0080_shadow_tig_asia"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_trade_observations (
            id uuid PRIMARY KEY,
            message_id uuid NOT NULL,
            source_id uuid NOT NULL,
            revision_index integer NOT NULL CHECK (revision_index >= 0),
            observed_at timestamptz NOT NULL,
            decision varchar(24) NOT NULL CHECK (
                decision IN ('new_trade','trade_update','preparation','chatter','non_actionable')
            ),
            action varchar(24) NOT NULL CHECK (
                action IN ('execute','skip','ignore','apply_update')
            ),
            executable boolean NOT NULL,
            outcome_reason varchar(64) NOT NULL,
            symbol varchar(24),
            side varchar(8) CHECK (side IS NULL OR side IN ('BUY','SELL')),
            order_type varchar(24),
            entry_low numeric,
            entry_high numeric,
            stop_loss numeric,
            take_profits jsonb NOT NULL DEFAULT '[]'::jsonb,
            tp_open boolean NOT NULL DEFAULT false,
            update_type varchar(32),
            update_target varchar(32),
            update_value numeric,
            confidence numeric CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            model varchar(64),
            decision_source varchar(32) NOT NULL,
            signal_id uuid,
            raw_text_sha256 varchar(64) NOT NULL,
            -- An executable observation is exactly one that produced a Signal. Research
            -- rows can never claim executability they did not have.
            CHECK ((executable AND action IN ('execute','apply_update')) OR NOT executable),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_execution_affected boolean NOT NULL DEFAULT false
                CHECK (NOT live_execution_affected),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(message_id,revision_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_trade_observations_source_time "
        "ON provider_trade_observations(source_id,observed_at)"
    )
    op.execute(
        "CREATE INDEX ix_provider_trade_observations_outcome "
        "ON provider_trade_observations(source_id,decision,executable,outcome_reason)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION provider_trade_observations_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Provider trade observations are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_trade_observations_append_only
        BEFORE UPDATE OR DELETE ON provider_trade_observations
        FOR EACH ROW EXECUTE FUNCTION provider_trade_observations_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_trade_observations")
    op.execute("DROP FUNCTION IF EXISTS provider_trade_observations_append_only()")
