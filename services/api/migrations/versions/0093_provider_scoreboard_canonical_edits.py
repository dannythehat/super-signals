"""Canonicalize edited provider signals and separate settled from open P&L.

Revision ID: 0093_provider_scoreboard_canon
Revises: 0092_telegram_market_updates
Create Date: 2026-09-16

The first retrospective provider scoreboard treated every ``new_trade`` interpretation
row as an independent trade. Telegram providers frequently build one signal by editing
the same message, so that double-counted progressive revisions. Those revisions also
kept the original message ``observed_at`` timestamp, allowing later-added geometry to be
replayed against price action from before it existed.

This migration creates one canonical research snapshot per Telegram message: the first
revision that was complete enough to trade, timed at the moment that revision actually
existed. Later edits remain in the append-only evidence tables, but are not independent
trades in this retrospective benchmark.

The scoreboard also stops mixing ``open_at_window_end`` partial P&L into settled Net P&L.
Open partial P&L is exposed separately, normalized R-per-trade/R-per-leg metrics are
added, and edit-assembly metrics travel with each provider so AIDY can distinguish a
provider that posts a complete setup once from one that constructs it by editing a post.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0093_provider_scoreboard_canon"
down_revision: str | None = "0092_telegram_market_updates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RETROSPECTIVE_BENCHMARK_MODEL = "fixed_1000_10_per_tp_fair_v2_canonical_edits_v1"
SCORE_BASIS = "aidy_first_actionable_no_management_v1"

_CANONICAL_VIEW = r"""
CREATE VIEW provider_trade_canonical_observations AS
WITH actionable AS (
    SELECT
        o.id,
        o.message_id,
        o.source_id,
        o.revision_index,
        CASE
            WHEN o.revision_index = 0 THEN COALESCE(m.posted_at, o.created_at)
            ELSE COALESCE(mr.edited_at, mr.created_at, o.created_at)
        END AS effective_at,
        o.side,
        o.order_type,
        o.entry_low,
        o.entry_high,
        o.stop_loss,
        o.take_profits,
        ROW_NUMBER() OVER (
            PARTITION BY o.message_id
            ORDER BY o.revision_index ASC, o.created_at ASC, o.id ASC
        ) AS actionable_rank
    FROM provider_trade_observations o
    JOIN messages m ON m.id = o.message_id
    LEFT JOIN message_revisions mr
      ON mr.message_id = o.message_id
     AND mr.revision_index = o.revision_index
    WHERE o.decision = 'new_trade'
      AND o.side IS NOT NULL
      AND o.entry_low IS NOT NULL
      AND o.stop_loss IS NOT NULL
      AND jsonb_typeof(o.take_profits) = 'array'
      AND jsonb_array_length(o.take_profits) > 0
)
SELECT
    id,
    message_id,
    source_id,
    revision_index,
    effective_at AS observed_at,
    side,
    order_type,
    entry_low,
    entry_high,
    stop_loss,
    take_profits
FROM actionable
WHERE actionable_rank = 1;
"""

_NEW_SCOREBOARD = rf"""
CREATE VIEW provider_trade_scoreboard AS
WITH observed AS (
    SELECT
        source_id,
        COUNT(DISTINCT message_id) FILTER (WHERE decision = 'new_trade') AS trades_recorded,
        COUNT(DISTINCT message_id) FILTER (WHERE decision = 'trade_update') AS updates_recorded,
        MIN(observed_at) AS first_observed_at,
        MAX(observed_at) AS last_observed_at
    FROM provider_trade_observations
    GROUP BY source_id
), scorable AS (
    SELECT
        c.source_id,
        COUNT(*) AS trades_scorable,
        COUNT(*) FILTER (WHERE c.revision_index > 0) AS edit_assembled_trades,
        AVG(EXTRACT(EPOCH FROM (c.observed_at - m.posted_at))) AS avg_actionable_delay_seconds
    FROM provider_trade_canonical_observations c
    JOIN messages m ON m.id = c.message_id
    GROUP BY c.source_id
), scored AS (
    SELECT
        ps.source_id,
        COUNT(*) FILTER (WHERE ps.outcome = 'won') AS wins,
        COUNT(*) FILTER (WHERE ps.outcome = 'lost') AS losses,
        COUNT(*) FILTER (WHERE ps.outcome = 'breakeven') AS breakeven,
        COUNT(*) FILTER (WHERE ps.outcome = 'never_entered') AS never_entered,
        COUNT(*) FILTER (WHERE ps.outcome = 'open_at_window_end') AS still_open,
        COUNT(*) FILTER (WHERE ps.outcome = 'unresolvable') AS not_scored,
        COUNT(*) FILTER (
            WHERE ps.outcome IN ('won', 'lost', 'breakeven')
        ) AS resolved,
        COALESCE(SUM(ps.net_pnl_usd) FILTER (
            WHERE ps.outcome IN ('won', 'lost', 'breakeven')
        ), 0::numeric) AS resolved_net_pnl_usd,
        COALESCE(SUM(ps.net_pnl_usd) FILTER (
            WHERE ps.outcome = 'open_at_window_end'
        ), 0::numeric) AS open_partial_pnl_usd,
        COALESCE(SUM(ps.realized_r) FILTER (
            WHERE ps.outcome IN ('won', 'lost', 'breakeven')
        ), 0::numeric) AS resolved_total_r,
        COALESCE(SUM(ps.legs_total) FILTER (
            WHERE ps.outcome IN ('won', 'lost', 'breakeven')
        ), 0::bigint) AS resolved_legs_total,
        MAX(ps.scored_at) AS last_scored_at
    FROM provider_trade_scores ps
    JOIN provider_trade_canonical_observations c ON c.id = ps.observation_id
    WHERE ps.benchmark_model = '{RETROSPECTIVE_BENCHMARK_MODEL}'
    GROUP BY ps.source_id
)
SELECT
    s.id AS source_id,
    COALESCE(NULLIF(s.chat_title, ''), s.source_alias) AS provider,
    s.status,
    o.trades_recorded,
    COALESCE(c.trades_scorable, 0::bigint) AS trades_scorable,
    COALESCE(sc.resolved, 0::bigint) AS trades_resolved,
    COALESCE(sc.wins, 0::bigint) AS wins,
    COALESCE(sc.losses, 0::bigint) AS losses,
    COALESCE(sc.breakeven, 0::bigint) AS breakeven,
    COALESCE(sc.never_entered, 0::bigint) AS never_entered,
    COALESCE(sc.still_open, 0::bigint) AS still_open,
    COALESCE(sc.not_scored, 0::bigint) AS not_scored,
    CASE
        WHEN COALESCE(sc.resolved, 0::bigint) > 0
        THEN ROUND(100.0 * sc.wins::numeric / sc.resolved::numeric, 1)
        ELSE NULL::numeric
    END AS win_rate_pct,
    COALESCE(sc.resolved_net_pnl_usd, 0::numeric) AS net_pnl_usd,
    CASE
        WHEN COALESCE(sc.resolved, 0::bigint) > 0
        THEN ROUND(sc.resolved_net_pnl_usd / sc.resolved::numeric, 2)
        ELSE NULL::numeric
    END AS avg_pnl_usd,
    COALESCE(sc.resolved_total_r, 0::numeric) AS total_r,
    CASE
        WHEN COALESCE(c.trades_scorable, 0::bigint) > 0
        THEN ROUND(100.0 * COALESCE(sc.resolved, 0::bigint)::numeric / c.trades_scorable::numeric, 1)
        ELSE NULL::numeric
    END AS scored_coverage_pct,
    CASE
        WHEN (o.trades_recorded + o.updates_recorded) > 0
        THEN ROUND(100.0 * o.updates_recorded::numeric /
             (o.trades_recorded + o.updates_recorded)::numeric, 1)
        ELSE NULL::numeric
    END AS update_rate_pct,
    o.first_observed_at,
    o.last_observed_at,
    sc.last_scored_at,
    COALESCE(sc.open_partial_pnl_usd, 0::numeric) AS open_partial_pnl_usd,
    CASE
        WHEN COALESCE(sc.resolved, 0::bigint) > 0
        THEN ROUND(sc.resolved_total_r / sc.resolved::numeric, 4)
        ELSE NULL::numeric
    END AS avg_r_per_trade,
    CASE
        WHEN COALESCE(sc.resolved_legs_total, 0::bigint) > 0
        THEN ROUND(sc.resolved_total_r / sc.resolved_legs_total::numeric, 4)
        ELSE NULL::numeric
    END AS avg_r_per_leg,
    CASE
        WHEN COALESCE(c.trades_scorable, 0::bigint) > 0
        THEN ROUND(100.0 * COALESCE(c.edit_assembled_trades, 0::bigint)::numeric /
             c.trades_scorable::numeric, 1)
        ELSE NULL::numeric
    END AS edit_assembled_pct,
    CASE
        WHEN c.avg_actionable_delay_seconds IS NOT NULL
        THEN ROUND(c.avg_actionable_delay_seconds::numeric, 1)
        ELSE NULL::numeric
    END AS avg_actionable_delay_seconds,
    '{SCORE_BASIS}'::text AS score_basis,
    '{RETROSPECTIVE_BENCHMARK_MODEL}'::text AS benchmark_model
FROM sources s
JOIN observed o ON o.source_id = s.id
LEFT JOIN scorable c ON c.source_id = s.id
LEFT JOIN scored sc ON sc.source_id = s.id;
"""

_OLD_SCOREBOARD = r"""
CREATE VIEW provider_trade_scoreboard AS
WITH observed AS (
    SELECT provider_trade_observations.source_id,
        count(*) FILTER (WHERE provider_trade_observations.decision::text = 'new_trade'::text) AS trades_recorded,
        count(*) FILTER (WHERE provider_trade_observations.decision::text = 'trade_update'::text) AS updates_recorded,
        count(*) FILTER (WHERE provider_trade_observations.decision::text = 'new_trade'::text
            AND provider_trade_observations.side IS NOT NULL
            AND provider_trade_observations.entry_low IS NOT NULL
            AND provider_trade_observations.stop_loss IS NOT NULL
            AND jsonb_array_length(provider_trade_observations.take_profits) > 0) AS trades_scorable,
        min(provider_trade_observations.observed_at) AS first_observed_at,
        max(provider_trade_observations.observed_at) AS last_observed_at
    FROM provider_trade_observations
    GROUP BY provider_trade_observations.source_id
), scored AS (
    SELECT provider_trade_scores.source_id,
        count(*) FILTER (WHERE provider_trade_scores.outcome::text = 'won'::text) AS wins,
        count(*) FILTER (WHERE provider_trade_scores.outcome::text = 'lost'::text) AS losses,
        count(*) FILTER (WHERE provider_trade_scores.outcome::text = 'breakeven'::text) AS breakeven,
        count(*) FILTER (WHERE provider_trade_scores.outcome::text = 'never_entered'::text) AS never_entered,
        count(*) FILTER (WHERE provider_trade_scores.outcome::text = 'open_at_window_end'::text) AS still_open,
        count(*) FILTER (WHERE provider_trade_scores.outcome::text = 'unresolvable'::text) AS not_scored,
        count(*) FILTER (WHERE provider_trade_scores.outcome::text = ANY (ARRAY['won'::character varying, 'lost'::character varying, 'breakeven'::character varying]::text[])) AS resolved,
        COALESCE(sum(provider_trade_scores.net_pnl_usd), 0::numeric) AS net_pnl_usd,
        COALESCE(sum(provider_trade_scores.realized_r), 0::numeric) AS total_r,
        max(provider_trade_scores.scored_at) AS last_scored_at
    FROM provider_trade_scores
    GROUP BY provider_trade_scores.source_id
)
SELECT s.id AS source_id,
    COALESCE(NULLIF(s.chat_title::text, ''::text), s.source_alias::text) AS provider,
    s.status,
    o.trades_recorded,
    o.trades_scorable,
    COALESCE(sc.resolved, 0::bigint) AS trades_resolved,
    COALESCE(sc.wins, 0::bigint) AS wins,
    COALESCE(sc.losses, 0::bigint) AS losses,
    COALESCE(sc.breakeven, 0::bigint) AS breakeven,
    COALESCE(sc.never_entered, 0::bigint) AS never_entered,
    COALESCE(sc.still_open, 0::bigint) AS still_open,
    COALESCE(sc.not_scored, 0::bigint) AS not_scored,
    CASE WHEN COALESCE(sc.resolved, 0::bigint) > 0
        THEN round(100.0 * sc.wins::numeric / sc.resolved::numeric, 1)
        ELSE NULL::numeric END AS win_rate_pct,
    COALESCE(sc.net_pnl_usd, 0::numeric) AS net_pnl_usd,
    CASE WHEN COALESCE(sc.resolved, 0::bigint) > 0
        THEN round(sc.net_pnl_usd / sc.resolved::numeric, 2)
        ELSE NULL::numeric END AS avg_pnl_usd,
    COALESCE(sc.total_r, 0::numeric) AS total_r,
    CASE WHEN o.trades_scorable > 0
        THEN round(100.0 * COALESCE(sc.resolved, 0::bigint)::numeric / o.trades_scorable::numeric, 1)
        ELSE NULL::numeric END AS scored_coverage_pct,
    CASE WHEN (o.trades_recorded + o.updates_recorded) > 0
        THEN round(100.0 * o.updates_recorded::numeric / (o.trades_recorded + o.updates_recorded)::numeric, 1)
        ELSE NULL::numeric END AS update_rate_pct,
    o.first_observed_at,
    o.last_observed_at,
    sc.last_scored_at
FROM sources s
JOIN observed o ON o.source_id = s.id
LEFT JOIN scored sc ON sc.source_id = s.id;
"""


def upgrade() -> None:
    op.execute(_CANONICAL_VIEW)
    op.execute("DROP VIEW provider_trade_scoreboard")
    op.execute(_NEW_SCOREBOARD)


def downgrade() -> None:
    op.execute("DROP VIEW provider_trade_scoreboard")
    op.execute(_OLD_SCOREBOARD)
    op.execute("DROP VIEW provider_trade_canonical_observations")
