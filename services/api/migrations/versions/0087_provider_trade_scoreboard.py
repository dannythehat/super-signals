"""Read the per-provider answer to "does this group make money" in one place.

The figures are research, from ``provider_trade_scores``, and the view exists so that
the caveats travel with them instead of being remembered separately.

Three columns are there to stop a number being read as more than it is:

``scored_coverage_pct`` says how much of a provider's catalogue the figure actually
rests on. A group with four scored trades out of two hundred has a P&L, and it means
almost nothing.

``update_rate_pct`` says how much of their traffic is management -- stop moves, partial
closes, early exits. Scoring does not model management, so every trade runs to its stop,
its targets or the end of the follow window. A group that actively protects trades
scores worse here than it deserves, and this column is what says by how much to
discount the verdict.

``never_entered`` separates trades the market never reached from trades that lost. A
provider posting limits that rarely fill is a different problem from one whose trades
fill and fail, and collapsing the two would hide which.

Win rate is deliberately taken over resolved trades only. Counting a trade still running
as a loss, or an unfilled limit as a win, would flatter or damn a provider by an
accident of when we looked.

Revision ID: 0087_provider_trade_scoreboard
Revises: 0086_provider_trade_scores
Create Date: 2026-09-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0087_provider_scoreboard"
down_revision: str | None = "0086_provider_trade_scores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW provider_trade_scoreboard AS
        WITH observed AS (
            SELECT source_id,
                   count(*) FILTER (WHERE decision='new_trade') AS trades_recorded,
                   count(*) FILTER (WHERE decision='trade_update') AS updates_recorded,
                   count(*) FILTER (
                       WHERE decision='new_trade' AND side IS NOT NULL
                         AND entry_low IS NOT NULL AND stop_loss IS NOT NULL
                         AND jsonb_array_length(take_profits) > 0
                   ) AS trades_scorable,
                   min(observed_at) AS first_observed_at,
                   max(observed_at) AS last_observed_at
            FROM provider_trade_observations
            GROUP BY source_id
        ),
        scored AS (
            SELECT source_id,
                   count(*) FILTER (WHERE outcome='won') AS wins,
                   count(*) FILTER (WHERE outcome='lost') AS losses,
                   count(*) FILTER (WHERE outcome='breakeven') AS breakeven,
                   count(*) FILTER (WHERE outcome='never_entered') AS never_entered,
                   count(*) FILTER (WHERE outcome='open_at_window_end') AS still_open,
                   count(*) FILTER (WHERE outcome='unresolvable') AS not_scored,
                   count(*) FILTER (
                       WHERE outcome IN ('won','lost','breakeven')
                   ) AS resolved,
                   COALESCE(sum(net_pnl_usd), 0) AS net_pnl_usd,
                   COALESCE(sum(realized_r), 0) AS total_r,
                   max(scored_at) AS last_scored_at
            FROM provider_trade_scores
            GROUP BY source_id
        )
        SELECT
            s.id AS source_id,
            COALESCE(NULLIF(s.chat_title,''), s.source_alias) AS provider,
            s.status,
            o.trades_recorded,
            o.trades_scorable,
            COALESCE(sc.resolved, 0) AS trades_resolved,
            COALESCE(sc.wins, 0) AS wins,
            COALESCE(sc.losses, 0) AS losses,
            COALESCE(sc.breakeven, 0) AS breakeven,
            COALESCE(sc.never_entered, 0) AS never_entered,
            COALESCE(sc.still_open, 0) AS still_open,
            COALESCE(sc.not_scored, 0) AS not_scored,
            CASE WHEN COALESCE(sc.resolved,0) > 0
                 THEN round(100.0 * sc.wins / sc.resolved, 1) END AS win_rate_pct,
            COALESCE(sc.net_pnl_usd, 0) AS net_pnl_usd,
            CASE WHEN COALESCE(sc.resolved,0) > 0
                 THEN round(sc.net_pnl_usd / sc.resolved, 2) END AS avg_pnl_usd,
            COALESCE(sc.total_r, 0) AS total_r,
            -- How much of the catalogue the P&L above actually rests on.
            CASE WHEN o.trades_scorable > 0
                 THEN round(100.0 * COALESCE(sc.resolved,0) / o.trades_scorable, 1)
                 END AS scored_coverage_pct,
            -- How much of their traffic is management the scoring does not model.
            CASE WHEN (o.trades_recorded + o.updates_recorded) > 0
                 THEN round(
                     100.0 * o.updates_recorded
                     / (o.trades_recorded + o.updates_recorded), 1)
                 END AS update_rate_pct,
            o.first_observed_at,
            o.last_observed_at,
            sc.last_scored_at
        FROM sources s
        JOIN observed o ON o.source_id = s.id
        LEFT JOIN scored sc ON sc.source_id = s.id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_trade_scoreboard")
