"""Give AIDY one immutable decision record per signal, across every decision class.

Day 16 and Day 20 each built a counterfactual table for one slice of this -- veto/filter
(`provider_veto_counterfactual_decisions`) and active management
(`provider_management_counterfactual_decisions`). Both are well designed and neither was
ever wired to a runner: zero rows in either, verified 2026-09-17. The owner's mandate
asks for one decision record that can express APPROVE, DENY, HOLD/NO_SECOND_ENTRY,
CONFLICT_DENY, CLOSE_EARLY and CONTINUE for the same signal over its lifetime, not two
siloed tables for two of those classes. This supersedes them rather than extending
either; nothing is lost since neither ever held real data.

The shape below keeps what Day 16/20 got right: a decision row freezes exactly what was
knowable at decision time and is never rewritten, an outcome row is written later and
scores the decision against a fixed baseline, and both are append-only and structurally
research-only. `evidence_digest` is what lets a later reader confirm a decision was not
retrospectively made to look smarter than it was.

Revision ID: 0088_aidy_decision_ledger
Revises: 0087_provider_scoreboard
Create Date: 2026-09-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0088_aidy_decision_ledger"
down_revision: str | None = "0087_provider_scoreboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE aidy_decisions (
            id uuid PRIMARY KEY,
            observation_id uuid NOT NULL
                REFERENCES provider_trade_observations(id) ON DELETE CASCADE,
            source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            signal_posted_at timestamptz NOT NULL,
            decided_at timestamptz NOT NULL,
            decision_class varchar(24) NOT NULL CHECK (
                decision_class IN (
                    'approve','deny','hold_no_second_entry','conflict_deny',
                    'close_early','continue'
                )
            ),
            -- Structured, not free text, so a reason is queryable across the whole
            -- ledger rather than only readable one row at a time.
            reasons jsonb NOT NULL,
            -- Null is honest for a purely deterministic rule (duplicate/conflict
            -- checks are yes/no, not graded). Only a reasoning model's output ever
            -- carries a confidence figure, and it is never invented to fill the column.
            confidence numeric CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            model_version varchar(64) NOT NULL,
            rule_version varchar(96) NOT NULL,
            -- Hash of the exact inputs the decision was made from: the observation,
            -- the scoreboard/context rows read, the open-exposure snapshot. Lets a
            -- later reader verify what AIDY actually knew rather than trusting a
            -- narrative reconstruction of it.
            evidence_digest varchar(64) NOT NULL,
            market_snapshot_id varchar(64),
            duplicate_of_decision_id uuid REFERENCES aidy_decisions(id),
            conflicts_with_decision_id uuid REFERENCES aidy_decisions(id),
            -- What actually happened downstream. Always 'none' while no decision
            -- class holds live authority; the column exists so graduating a class
            -- later is a value change here, not a schema change.
            resulting_action varchar(24) NOT NULL DEFAULT 'none' CHECK (
                resulting_action IN ('none','executed','blocked','closed')
            ),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(observation_id, decision_class, rule_version),
            CHECK (decided_at >= signal_posted_at)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_aidy_decisions_source_time "
        "ON aidy_decisions(source_id,signal_posted_at)"
    )
    op.execute(
        "CREATE INDEX ix_aidy_decisions_class "
        "ON aidy_decisions(decision_class,decided_at)"
    )
    op.execute(
        """
        CREATE TABLE aidy_decision_outcomes (
            id uuid PRIMARY KEY,
            decision_id uuid NOT NULL UNIQUE
                REFERENCES aidy_decisions(id) ON DELETE CASCADE,
            -- What the provider's own plan, unmodified, would have realised --
            -- the counterfactual baseline every decision is judged against.
            baseline_pnl_usd numeric NOT NULL,
            baseline_realized_r numeric NOT NULL,
            -- What actually happened under AIDY's decision. For 'approve'/'continue'
            -- this equals the baseline today, because AIDY holds no authority to
            -- change the outcome yet; the column is not collapsed into the baseline
            -- one so that stays true the moment a class graduates.
            actual_pnl_usd numeric NOT NULL,
            actual_realized_r numeric NOT NULL,
            -- The number the owner actually asked for: money made or saved because
            -- AIDY intervened, versus the fixed baseline. Never claimed as an
            -- avoided loss unless the baseline path proves the loss would have
            -- happened -- classify() below enforces that at write time.
            decision_delta_usd numeric NOT NULL,
            resolved_at timestamptz NOT NULL,
            resolution varchar(24) NOT NULL CHECK (
                resolution IN ('confirmed_helped','confirmed_hurt','neutral','open_at_window_end')
            ),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_aidy_decision_outcomes_resolution "
        "ON aidy_decision_outcomes(resolution,resolved_at)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aidy_decisions_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'AIDY decisions are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_aidy_decisions_append_only
        BEFORE UPDATE OR DELETE ON aidy_decisions
        FOR EACH ROW EXECUTE FUNCTION aidy_decisions_append_only()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aidy_decision_outcomes_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'AIDY decision outcomes are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_aidy_decision_outcomes_append_only
        BEFORE UPDATE OR DELETE ON aidy_decision_outcomes
        FOR EACH ROW EXECUTE FUNCTION aidy_decision_outcomes_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS aidy_decision_outcomes")
    op.execute("DROP TABLE IF EXISTS aidy_decisions")
    op.execute("DROP FUNCTION IF EXISTS aidy_decision_outcomes_append_only()")
    op.execute("DROP FUNCTION IF EXISTS aidy_decisions_append_only()")
