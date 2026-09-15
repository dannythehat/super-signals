"""Score recorded provider trades against price history, separately from forward evidence.

``score_eligibility`` admits only ``quote_mode='aidy_m1'`` for scalper, intraday and
swing styles -- bars AIDY genuinely observed at the time. That rule is right and is not
relaxed here: a provider may only be *promoted* on evidence the system actually saw, and
``provider_day13_runtime`` selects on ``score_eligible`` to enforce it.

"Which of these groups makes money" is a different question, and answering it from
``shadow_trades`` cannot work. Shadow trades only exist where a trade was also
mirrorable, so on 2026-09-15 the three largest providers by volume have none at all:
TDC V2 has 565 recorded trades and 0 shadow trades, The Gold Club 508 and 0, GOLD VIP
92 and 0. A scoreboard built on shadow trades would be silent about exactly the groups
it most needs to judge.

``provider_trade_observations`` holds every trade the interpreter understood, executable
or not -- 2,320 of them carry a side, an entry, a stop and at least one target, which is
everything scoring needs. So scores are keyed on the observation.

Rows here are research. ``forward_evidence_eligible`` is CHECK-constrained false, the
promotion gates never read this table, and a score is derived rather than observed: it
is recomputed as missing history is backfilled, which is why this table is not
append-only. ``scored_at`` and ``missing_minutes`` are what make a stale or thin figure
visible instead of silently wrong.

Revision ID: 0086_provider_trade_scores
Revises: 0085_provider_behaviour_profile
Create Date: 2026-09-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0086_provider_trade_scores"
down_revision: str | None = "0085_provider_behaviour_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_trade_scores (
            id uuid PRIMARY KEY,
            observation_id uuid NOT NULL UNIQUE
                REFERENCES provider_trade_observations(id) ON DELETE CASCADE,
            source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            scored_at timestamptz NOT NULL DEFAULT now(),
            benchmark_model varchar(64) NOT NULL,
            quote_mode varchar(32) NOT NULL DEFAULT 'aidy_m1_retrospective'
                CHECK (quote_mode = 'aidy_m1_retrospective'),
            -- How the entry was read. A posted range is filled at the edge least
            -- favourable to the trader, so the convention never flatters a provider.
            entry_convention varchar(16) NOT NULL
                CHECK (entry_convention IN ('zone','market')),
            outcome varchar(24) NOT NULL CHECK (
                outcome IN (
                    'won','lost','breakeven','never_entered',
                    'open_at_window_end','unresolvable'
                )
            ),
            unresolvable_reason varchar(120),
            entry_price numeric,
            net_pnl_usd numeric,
            realized_r numeric,
            legs_resolved integer NOT NULL DEFAULT 0 CHECK (legs_resolved >= 0),
            legs_total integer NOT NULL DEFAULT 0 CHECK (legs_total >= 0),
            first_bar_utc timestamptz,
            last_bar_utc timestamptz,
            bars_replayed integer NOT NULL DEFAULT 0 CHECK (bars_replayed >= 0),
            missing_minutes integer NOT NULL DEFAULT 0 CHECK (missing_minutes >= 0),
            -- An unresolvable row states why and carries no figure; a resolved one
            -- carries a figure and no excuse. Neither shape can be half-filled.
            CHECK (
                (outcome = 'unresolvable'
                 AND net_pnl_usd IS NULL AND unresolvable_reason IS NOT NULL)
                OR (outcome <> 'unresolvable' AND unresolvable_reason IS NULL)
            ),
            -- A trade that never entered has no P&L either, and saying so is not the
            -- same as failing to score it.
            CHECK (outcome <> 'never_entered' OR net_pnl_usd IS NULL),
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            forward_evidence_eligible boolean NOT NULL DEFAULT false
                CHECK (NOT forward_evidence_eligible),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_trade_scores_source "
        "ON provider_trade_scores(source_id,outcome)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_trade_scores")
