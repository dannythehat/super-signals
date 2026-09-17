"""Give AIDY a persistent, computed answer to "what separates this provider's wins from its losses."

The owner asked directly: does AIDY actually understand each trader, what separates winners
from losers, and does it use that? Until now the honest answer was split in an unhelpful way --
AIDY tracked every trade and decided on every signal, but the only place "why do they win" was
answered was either an ad-hoc query someone ran by hand, or the Day 13 conditional-hypothesis
registry, which is deliberately statistically rigorous and therefore requires real forward
evidence to accumulate over weeks before it can say anything at all.

This fills the honest middle ground: a descriptive fingerprint per provider, computed from
trades that have already resolved (no need to wait for new forward evidence), comparing winning
signals against losing ones from the same provider -- stop distance, planned reward:risk, which
side and session actually works for them. Explicitly labelled descriptive, not causal or
statistically certified; Day 13 remains the only place a "proven pattern" claim can come from.
The point of building this is that it gets read, not just computed -- aidy_reasoning_engine.py
is wired to pull a provider's latest fingerprint into its prompt, so a new signal gets judged
against how *this specific provider's* wins and losses actually look, not generic rules alone.

Revision ID: 0091_provider_trade_fingerprints
Revises: 0090_aidy_reasoning_annotations
Create Date: 2026-09-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0091_provider_trade_fingerprints"
down_revision: str | None = "0090_aidy_reasoning_annotations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_trade_fingerprints (
            id uuid PRIMARY KEY,
            source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            computed_at timestamptz NOT NULL DEFAULT now(),
            -- Pulled from provider_research_profiles.style at compute time -- part of the
            -- profile, not just the win/loss geometry split.
            trading_style varchar(32),
            trades_resolved integer NOT NULL CHECK (trades_resolved >= 0),
            wins integer NOT NULL CHECK (wins >= 0),
            losses integer NOT NULL CHECK (losses >= 0),
            win_rate_pct numeric,
            -- Stop distance and planned reward:risk, compared won vs lost -- the geometry
            -- question: do this provider's winners look structurally different from their
            -- losers, in their own history, not a generic rule.
            avg_stop_distance_won numeric,
            avg_stop_distance_lost numeric,
            avg_planned_rr_won numeric,
            avg_planned_rr_lost numeric,
            geometry_sample_met boolean NOT NULL,
            best_side varchar(8),
            best_side_win_rate_pct numeric,
            best_side_trades integer,
            worst_side varchar(8),
            worst_side_win_rate_pct numeric,
            worst_side_trades integer,
            best_session varchar(32),
            best_session_win_rate_pct numeric,
            best_session_trades integer,
            worst_session varchar(32),
            worst_session_win_rate_pct numeric,
            worst_session_trades integer,
            cohort_sample_met boolean NOT NULL,
            -- Plain-English summary, fed directly into the reasoning prompt and readable on
            -- its own -- the whole point is this gets used, not filed away as numbers alone.
            summary text NOT NULL,
            descriptive_only boolean NOT NULL DEFAULT true CHECK (descriptive_only),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_trade_fingerprints_source_time "
        "ON provider_trade_fingerprints(source_id, computed_at DESC)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION provider_trade_fingerprints_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'provider trade fingerprints are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_trade_fingerprints_append_only
        BEFORE UPDATE OR DELETE ON provider_trade_fingerprints
        FOR EACH ROW EXECUTE FUNCTION provider_trade_fingerprints_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_trade_fingerprints")
    op.execute("DROP FUNCTION IF EXISTS provider_trade_fingerprints_append_only()")
