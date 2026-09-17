"""Read a provider's record broken out by side, session and weekday, not blended into one number.

``provider_trade_scoreboard`` answers "does this group make money" with a single win
rate and average P&L per provider. That number can hide the actual shape of a provider's
edge: a group that is genuinely good on BUY signals during London and genuinely bad on
SELL signals overnight reads, blended, as merely mediocre -- exactly the kind of
conditional pattern the owner's 2026-09-16 directive asks AIDY to be able to see
("direction, session/time, weekday... overall provider win rate is not enough").

This view does not change what the Decision Ledger reads today -- ``aidy_decision_engine``
still evaluates the blended scoreboard. Wiring cohort-specific evidence into a live
decision is a separate, later step: it needs its own judgement call about how a strong
cohort signal should weigh against a weak blended one, which deserves its own review
rather than riding along with a preparatory view.

``session`` is a rough, non-overlapping bucketing of UTC hour-of-day using the commonly
cited forex session windows (Asia/London/London-New-York overlap/New York/late), not a
precise market-microstructure classification -- good enough to ask "does this group do
better in the London hours," not to model liquidity. ``weekday`` is computed in
Europe/Sofia to match the existing weekly trading-freeze convention elsewhere in this
codebase, so "Monday" here means the same Monday the freeze logic means.

Every cohort cell inherits the parent view's caveats and then some: a cohort this narrow
reaches a usable sample size far slower than the blended provider figure does, so a
cohort of 3 trades is not evidence of anything and must not be read as if it were --
that is why ``trades_resolved`` and ``scored_coverage_pct`` are still carried per cell,
not dropped for looking clean.

Revision ID: 0089_provider_scoreboard_cohorts
Revises: 0088_aidy_decision_ledger
Create Date: 2026-09-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0089_provider_scoreboard_cohorts"
down_revision: str | None = "0088_aidy_decision_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW provider_trade_scoreboard_by_cohort AS
        WITH cohorted AS (
            SELECT
                o.id AS observation_id,
                o.source_id,
                o.side,
                CASE
                    WHEN EXTRACT(HOUR FROM o.observed_at AT TIME ZONE 'UTC') < 7
                        THEN 'asia'
                    WHEN EXTRACT(HOUR FROM o.observed_at AT TIME ZONE 'UTC') < 13
                        THEN 'london'
                    WHEN EXTRACT(HOUR FROM o.observed_at AT TIME ZONE 'UTC') < 16
                        THEN 'london_new_york_overlap'
                    WHEN EXTRACT(HOUR FROM o.observed_at AT TIME ZONE 'UTC') < 21
                        THEN 'new_york'
                    ELSE 'late'
                END AS session,
                trim(to_char(o.observed_at AT TIME ZONE 'Europe/Sofia', 'Day')) AS weekday,
                sc.outcome,
                sc.net_pnl_usd
            FROM provider_trade_observations o
            LEFT JOIN provider_trade_scores sc ON sc.observation_id = o.id
            WHERE o.decision = 'new_trade'
              AND o.side IS NOT NULL
              AND o.entry_low IS NOT NULL
              AND o.stop_loss IS NOT NULL
              AND jsonb_array_length(o.take_profits) > 0
        )
        SELECT
            c.source_id,
            COALESCE(NULLIF(s.chat_title,''), s.source_alias) AS provider,
            c.side,
            c.session,
            c.weekday,
            count(*) AS trades_scorable,
            count(*) FILTER (WHERE c.outcome IN ('won','lost','breakeven')) AS trades_resolved,
            count(*) FILTER (WHERE c.outcome = 'won') AS wins,
            count(*) FILTER (WHERE c.outcome = 'lost') AS losses,
            count(*) FILTER (WHERE c.outcome = 'breakeven') AS breakeven,
            count(*) FILTER (WHERE c.outcome = 'never_entered') AS never_entered,
            CASE WHEN count(*) FILTER (WHERE c.outcome IN ('won','lost','breakeven')) > 0
                 THEN round(
                     100.0 * count(*) FILTER (WHERE c.outcome = 'won')
                     / count(*) FILTER (WHERE c.outcome IN ('won','lost','breakeven')), 1)
                 END AS win_rate_pct,
            COALESCE(
                sum(c.net_pnl_usd) FILTER (WHERE c.outcome IN ('won','lost','breakeven')), 0
            ) AS net_pnl_usd,
            CASE WHEN count(*) FILTER (WHERE c.outcome IN ('won','lost','breakeven')) > 0
                 THEN round(
                     COALESCE(
                         sum(c.net_pnl_usd)
                             FILTER (WHERE c.outcome IN ('won','lost','breakeven')), 0
                     ) / count(*) FILTER (WHERE c.outcome IN ('won','lost','breakeven')), 2)
                 END AS avg_pnl_usd,
            CASE WHEN count(*) > 0
                 THEN round(
                     100.0 * count(*) FILTER (WHERE c.outcome IN ('won','lost','breakeven'))
                     / count(*), 1)
                 END AS scored_coverage_pct
        FROM cohorted c
        JOIN sources s ON s.id = c.source_id
        GROUP BY c.source_id, s.chat_title, s.source_alias, c.side, c.session, c.weekday
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_trade_scoreboard_by_cohort")
