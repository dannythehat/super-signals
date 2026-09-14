"""Describe how each provider trades, from what they actually posted.

Shadow research exists to find which groups are worth following, and that needs a
description of each group before any judgement about it. Until observations were
recorded regardless of executability there was nothing to build one from: a group that
never publishes a stop loss produced no rows, so the single most useful fact about it
was also the reason it was invisible.

This view answers, per provider: how many trades they post, how many we could actually
mirror and why not, whether they publish stops and targets, how many targets, whether
they trade zones or exact prices, their directional bias, how they manage a trade once
open, and when they trade by session and weekday.

It deliberately says nothing about profitability. Outcomes require PIT resolution
against AIDY M1 truth, and asserting a P&L here from message content alone would invent
exactly the kind of unverified number this system is supposed to refuse. Profitability
joins the scoreboard when the resolver has scored the corpus, not before.

Revision ID: 0085_provider_behaviour_profile
Revises: 0084_declare_gold_shadow_sources
Create Date: 2026-09-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0085_provider_behaviour_profile"
down_revision: str | None = "0084_declare_gold_shadow_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIEW provider_behaviour_profile AS
        WITH trades AS (
            SELECT o.source_id,
                   count(*) AS trades,
                   count(*) FILTER (WHERE o.executable) AS mirrorable,
                   count(*) FILTER (WHERE o.stop_loss IS NOT NULL) AS with_stop,
                   count(*) FILTER (WHERE jsonb_array_length(o.take_profits) > 0) AS with_target,
                   avg(jsonb_array_length(o.take_profits)) AS avg_targets,
                   count(*) FILTER (
                       WHERE o.entry_low IS NOT NULL AND o.entry_high IS NOT NULL
                         AND o.entry_low <> o.entry_high
                   ) AS zone_entries,
                   count(*) FILTER (WHERE o.side = 'BUY') AS buys,
                   count(*) FILTER (WHERE o.tp_open) AS runners,
                   count(*) FILTER (WHERE extract(hour from o.observed_at) < 7) AS asia,
                   count(*) FILTER (
                       WHERE extract(hour from o.observed_at) BETWEEN 7 AND 12
                   ) AS london,
                   count(*) FILTER (WHERE extract(hour from o.observed_at) > 12) AS new_york,
                   count(*) FILTER (WHERE extract(isodow from o.observed_at) = 1) AS monday,
                   count(*) FILTER (WHERE extract(isodow from o.observed_at) = 5) AS friday,
                   count(DISTINCT o.observed_at::date) AS active_days,
                   min(o.observed_at) AS first_seen,
                   max(o.observed_at) AS last_seen
            FROM provider_trade_observations o
            WHERE o.decision = 'new_trade'
            GROUP BY o.source_id
        ), blocked AS (
            -- The most common reason a trade could not be mirrored is a fact about the
            -- provider, not about us: it is usually what they decline to publish.
            SELECT DISTINCT ON (source_id) source_id, outcome_reason, n
            FROM (
                SELECT source_id, outcome_reason, count(*) AS n
                FROM provider_trade_observations
                WHERE decision = 'new_trade' AND NOT executable
                GROUP BY source_id, outcome_reason
            ) ranked
            ORDER BY source_id, n DESC
        ), management AS (
            SELECT DISTINCT ON (source_id) source_id, update_type, n
            FROM (
                SELECT source_id, update_type, count(*) AS n
                FROM provider_trade_observations
                WHERE decision = 'trade_update' AND update_type IS NOT NULL
                GROUP BY source_id, update_type
            ) ranked
            ORDER BY source_id, n DESC
        ), managed AS (
            SELECT source_id, count(*) AS updates
            FROM provider_trade_observations
            WHERE decision = 'trade_update'
            GROUP BY source_id
        )
        SELECT
            s.id AS source_id,
            COALESCE(NULLIF(s.chat_title,''), s.source_alias) AS provider,
            s.status,
            s.declared_instrument,
            t.trades,
            t.mirrorable,
            round(100.0 * t.mirrorable / NULLIF(t.trades,0)) AS pct_mirrorable,
            b.outcome_reason AS main_blocker,
            round(100.0 * t.with_stop / NULLIF(t.trades,0)) AS pct_with_stop,
            round(100.0 * t.with_target / NULLIF(t.trades,0)) AS pct_with_target,
            round(t.avg_targets, 1) AS avg_targets,
            round(100.0 * t.zone_entries / NULLIF(t.trades,0)) AS pct_zone_entry,
            round(100.0 * t.buys / NULLIF(t.trades,0)) AS pct_buy,
            round(100.0 * t.runners / NULLIF(t.trades,0)) AS pct_runner,
            COALESCE(mu.updates, 0) AS management_messages,
            m.update_type AS main_management_action,
            round(100.0 * t.asia / NULLIF(t.trades,0)) AS pct_asia,
            round(100.0 * t.london / NULLIF(t.trades,0)) AS pct_london,
            round(100.0 * t.new_york / NULLIF(t.trades,0)) AS pct_new_york,
            round(100.0 * t.monday / NULLIF(t.trades,0)) AS pct_monday,
            round(100.0 * t.friday / NULLIF(t.trades,0)) AS pct_friday,
            t.active_days,
            round(t.trades::numeric / NULLIF(t.active_days,0), 1) AS trades_per_active_day,
            t.first_seen,
            t.last_seen
        FROM sources s
        JOIN trades t ON t.source_id = s.id
        LEFT JOIN blocked b ON b.source_id = s.id
        LEFT JOIN management m ON m.source_id = s.id
        LEFT JOIN managed mu ON mu.source_id = s.id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_behaviour_profile")
