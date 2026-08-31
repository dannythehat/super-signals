"""Add fair Provider Lab execution evidence and TP-level benchmark rows.

Revision ID: 0047_provider_fairness_v2
Revises: 0046_provider_benchmark_view
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_provider_fairness_v2"
down_revision: str | None = "0046_provider_benchmark_view"
branch_labels: str | Sequence[str] | None = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_benchmark_performance")
    op.drop_constraint("ck_shadow_benchmark_model", "shadow_trades", type_="check")

    op.alter_column(
        "shadow_trades",
        "benchmark_model",
        server_default="fixed_1000_10_per_tp_fair_v2",
    )
    op.execute(
        "UPDATE shadow_trades SET benchmark_model='fixed_1000_10_per_tp_fair_v2' "
        "WHERE benchmark_model='fixed_1000_10_per_tp_v1'"
    )
    op.create_check_constraint(
        "ck_shadow_benchmark_model",
        "shadow_trades",
        "benchmark_model IN ('fixed_1000_10_per_tp_fair_v2')",
    )

    op.add_column("shadow_trades", sa.Column("provider_style", sa.String(length=32), nullable=False, server_default="unknown"))
    op.add_column("shadow_trades", sa.Column("interpretation_readiness_at_entry", sa.Numeric(6, 5), nullable=False, server_default="0"))
    op.add_column("shadow_trades", sa.Column("signal_posted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("shadow_trades", sa.Column("session_bucket", sa.String(length=32), nullable=False, server_default="unknown"))
    op.add_column("shadow_trades", sa.Column("weekday_iso", sa.SmallInteger(), nullable=True))
    op.add_column("shadow_trades", sa.Column("quote_mode", sa.String(length=24), nullable=False, server_default="unobserved"))
    op.add_column("shadow_trades", sa.Column("score_eligible", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("shadow_trades", sa.Column("score_exclusion_reason", sa.String(length=80), nullable=True))
    op.add_column("shadow_trades", sa.Column("entry_delay_ms", sa.BigInteger(), nullable=True))
    op.add_column("shadow_trades", sa.Column("entry_spread", sa.Numeric(24, 10), nullable=True))
    op.add_column("shadow_trades", sa.Column("target_count", sa.Integer(), nullable=False, server_default="0"))

    op.create_check_constraint(
        "ck_shadow_fair_style",
        "shadow_trades",
        "provider_style IN ('scalper','intraday','swing_or_sparse','mixed','unknown')",
    )
    op.create_check_constraint(
        "ck_shadow_fair_readiness",
        "shadow_trades",
        "interpretation_readiness_at_entry >= 0 AND interpretation_readiness_at_entry <= 1",
    )
    op.create_check_constraint(
        "ck_shadow_fair_weekday",
        "shadow_trades",
        "weekday_iso IS NULL OR (weekday_iso >= 1 AND weekday_iso <= 7)",
    )
    op.create_check_constraint(
        "ck_shadow_fair_quote_mode",
        "shadow_trades",
        "quote_mode IN ('unobserved','snapshot_poll','stream_quote','stream_tick')",
    )
    op.create_check_constraint(
        "ck_shadow_fair_target_count",
        "shadow_trades",
        "target_count >= 0",
    )

    op.create_table(
        "shadow_trade_legs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("shadow_trade_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shadow_trades.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("signals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tp_index", sa.Integer(), nullable=False),
        sa.Column("target_price", sa.Numeric(24, 10), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("exit_reason", sa.String(length=40), nullable=True),
        sa.Column("exit_price", sa.Numeric(24, 10), nullable=True),
        sa.Column("quality_r", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("benchmark_pnl_usd", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("shadow_trade_id", "tp_index", name="uq_shadow_trade_leg_tp"),
        sa.CheckConstraint("tp_index >= 1", name="ck_shadow_leg_tp_index"),
        sa.CheckConstraint("status IN ('pending','open','closed','cancelled')", name="ck_shadow_leg_status"),
    )
    op.create_index("ix_shadow_legs_source_status", "shadow_trade_legs", ["source_id", "status"])
    op.create_index("ix_shadow_legs_signal", "shadow_trade_legs", ["signal_id"])

    op.execute(
        """
        CREATE VIEW provider_benchmark_performance AS
        WITH eligible_closed AS (
            SELECT
                t.id,
                t.source_id,
                t.closed_at,
                t.quality_r_multiple,
                t.benchmark_pnl_usd,
                SUM(t.benchmark_pnl_usd) OVER (
                    PARTITION BY t.source_id ORDER BY t.closed_at,t.id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS cumulative_pnl_usd
            FROM shadow_trades t
            WHERE t.status='closed'
              AND t.closed_at IS NOT NULL
              AND t.score_eligible
              AND t.benchmark_model='fixed_1000_10_per_tp_fair_v2'
        ), equity AS (
            SELECT e.*,
                   MAX(e.cumulative_pnl_usd) OVER (
                       PARTITION BY e.source_id ORDER BY e.closed_at,e.id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS running_peak_pnl_usd
            FROM eligible_closed e
        ), agg AS (
            SELECT
                source_id,
                COUNT(*)::integer AS closed_trades,
                COUNT(*) FILTER (WHERE quality_r_multiple>0)::integer AS wins,
                COUNT(*) FILTER (WHERE quality_r_multiple<0)::integer AS losses,
                COUNT(DISTINCT closed_at::date)::integer AS trading_days,
                COUNT(DISTINCT date_trunc('week',closed_at))::integer AS trading_weeks,
                COALESCE(SUM(quality_r_multiple),0)::numeric(18,6) AS total_r,
                COALESCE(AVG(quality_r_multiple),0)::numeric(18,6) AS average_r,
                COALESCE(SUM(benchmark_pnl_usd),0)::numeric(18,2) AS benchmark_pnl_usd,
                COALESCE(MAX(running_peak_pnl_usd-cumulative_pnl_usd),0)::numeric(18,2) AS max_drawdown_usd,
                COALESCE(SUM(quality_r_multiple) FILTER (WHERE quality_r_multiple>0),0)::numeric(18,6) AS gross_positive_r,
                ABS(COALESCE(SUM(quality_r_multiple) FILTER (WHERE quality_r_multiple<0),0))::numeric(18,6) AS gross_negative_r
            FROM equity GROUP BY source_id
        ), daily AS (
            SELECT source_id,closed_at::date AS day,SUM(benchmark_pnl_usd) AS day_pnl
            FROM eligible_closed GROUP BY source_id,closed_at::date
        ), day_agg AS (
            SELECT source_id,
                   COUNT(*) FILTER (WHERE day_pnl>0)::integer AS profitable_days,
                   COUNT(*) FILTER (WHERE day_pnl<0)::integer AS losing_days
            FROM daily GROUP BY source_id
        ), recent AS (
            SELECT source_id,
                   COUNT(*)::integer AS recent_closed_trades,
                   COALESCE(SUM(quality_r_multiple),0)::numeric(18,6) AS recent_total_r
            FROM eligible_closed
            WHERE closed_at >= now()-interval '30 days'
            GROUP BY source_id
        ), excluded AS (
            SELECT source_id,COUNT(*)::integer AS excluded_closed_trades
            FROM shadow_trades
            WHERE status='closed' AND NOT score_eligible
              AND benchmark_model='fixed_1000_10_per_tp_fair_v2'
            GROUP BY source_id
        )
        SELECT
            s.id AS source_id,
            COALESCE(s.chat_title,s.source_alias) AS title,
            s.status AS source_status,
            COALESCE(p.style,'unknown') AS style,
            COALESCE(p.research_state,'learning') AS research_state,
            COALESCE(p.interpretation_readiness,0)::numeric(6,5) AS interpretation_readiness,
            p.duplicate_of_source_id,
            COALESCE(a.closed_trades,0) AS closed_trades,
            COALESCE(x.excluded_closed_trades,0) AS excluded_closed_trades,
            COALESCE(a.wins,0) AS wins,
            COALESCE(a.losses,0) AS losses,
            COALESCE(a.trading_days,0) AS trading_days,
            COALESCE(a.trading_weeks,0) AS trading_weeks,
            COALESCE(d.profitable_days,0) AS profitable_days,
            COALESCE(d.losing_days,0) AS losing_days,
            CASE WHEN COALESCE(a.trading_days,0)>0
                 THEN ROUND((COALESCE(d.profitable_days,0)::numeric/a.trading_days)*100,2)
                 ELSE 0 END AS profitable_day_rate_percent,
            CASE WHEN COALESCE(a.closed_trades,0)>0
                 THEN ROUND((COALESCE(a.wins,0)::numeric/a.closed_trades)*100,2)
                 ELSE 0 END AS win_rate_percent,
            COALESCE(a.total_r,0)::numeric(18,6) AS total_r,
            COALESCE(a.average_r,0)::numeric(18,6) AS average_r,
            CASE WHEN COALESCE(a.gross_negative_r,0)=0 AND COALESCE(a.gross_positive_r,0)>0 THEN 999
                 WHEN COALESCE(a.gross_negative_r,0)=0 THEN 0
                 ELSE ROUND(a.gross_positive_r/a.gross_negative_r,3) END AS profit_factor,
            COALESCE(a.benchmark_pnl_usd,0)::numeric(18,2) AS benchmark_pnl_usd,
            (1000+COALESCE(a.benchmark_pnl_usd,0))::numeric(18,2) AS benchmark_balance_usd,
            ROUND((COALESCE(a.benchmark_pnl_usd,0)/1000)*100,2) AS benchmark_return_percent,
            COALESCE(a.max_drawdown_usd,0)::numeric(18,2) AS max_drawdown_usd,
            COALESCE(r.recent_closed_trades,0) AS recent_closed_trades,
            COALESCE(r.recent_total_r,0)::numeric(18,6) AS recent_total_r,
            CASE
                WHEN p.duplicate_of_source_id IS NOT NULL THEN 'DUPLICATE_REVIEW'
                WHEN COALESCE(p.interpretation_readiness,0)<0.90 THEN 'LEARNING'
                WHEN COALESCE(p.style,'unknown')='scalper'
                     AND (COALESCE(a.closed_trades,0)<100 OR COALESCE(a.trading_days,0)<20 OR COALESCE(a.trading_weeks,0)<4)
                     THEN 'INSUFFICIENT_EVIDENCE'
                WHEN COALESCE(p.style,'unknown')='intraday'
                     AND (COALESCE(a.closed_trades,0)<60 OR COALESCE(a.trading_days,0)<30 OR COALESCE(a.trading_weeks,0)<6)
                     THEN 'INSUFFICIENT_EVIDENCE'
                WHEN COALESCE(p.style,'unknown')='swing_or_sparse'
                     AND (COALESCE(a.closed_trades,0)<30 OR COALESCE(a.trading_days,0)<45 OR COALESCE(a.trading_weeks,0)<8)
                     THEN 'INSUFFICIENT_EVIDENCE'
                WHEN COALESCE(p.style,'unknown') IN ('mixed','unknown')
                     AND (COALESCE(a.closed_trades,0)<60 OR COALESCE(a.trading_days,0)<30 OR COALESCE(a.trading_weeks,0)<6)
                     THEN 'INSUFFICIENT_EVIDENCE'
                WHEN COALESCE(r.recent_closed_trades,0)>=10 AND COALESCE(r.recent_total_r,0)<0
                     THEN 'RECENT_DETERIORATION'
                WHEN COALESCE(a.total_r,0)>0
                     AND (CASE WHEN COALESCE(a.gross_negative_r,0)=0 AND COALESCE(a.gross_positive_r,0)>0 THEN 999
                               WHEN COALESCE(a.gross_negative_r,0)=0 THEN 0
                               ELSE a.gross_positive_r/a.gross_negative_r END)>=1.30
                     AND COALESCE(a.average_r,0)>0
                     AND COALESCE(a.max_drawdown_usd,0)<=150
                     AND COALESCE(p.interpretation_readiness,0)>=0.95
                     AND COALESCE(d.profitable_days,0) >= COALESCE(d.losing_days,0)
                     THEN 'QUALIFIED_EVIDENCE'
                WHEN COALESCE(a.total_r,0)>0 THEN 'PROMISING'
                ELSE 'NOT_PROVEN'
            END AS evidence_status
        FROM sources s
        LEFT JOIN provider_research_profiles p ON p.source_id=s.id
        LEFT JOIN agg a ON a.source_id=s.id
        LEFT JOIN day_agg d ON d.source_id=s.id
        LEFT JOIN recent r ON r.source_id=s.id
        LEFT JOIN excluded x ON x.source_id=s.id
        WHERE s.status<>'revoked'
        """
    )

    op.execute(
        """
        CREATE VIEW provider_benchmark_segments AS
        SELECT
            t.source_id,
            COALESCE(s.chat_title,s.source_alias) AS title,
            t.provider_style AS style,
            t.side,
            t.session_bucket,
            t.weekday_iso,
            l.tp_index,
            COUNT(*) FILTER (WHERE l.status='closed')::integer AS closed_legs,
            COUNT(*) FILTER (WHERE l.status='closed' AND l.quality_r>0)::integer AS winning_legs,
            ROUND(COALESCE(AVG(l.quality_r) FILTER (WHERE l.status='closed'),0),6) AS average_r,
            ROUND(COALESCE(SUM(l.quality_r) FILTER (WHERE l.status='closed'),0),6) AS total_r,
            ROUND(COALESCE(SUM(l.benchmark_pnl_usd) FILTER (WHERE l.status='closed'),0),2) AS pnl_usd,
            CASE
                WHEN ABS(COALESCE(SUM(l.quality_r) FILTER (WHERE l.status='closed' AND l.quality_r<0),0))=0
                     AND COALESCE(SUM(l.quality_r) FILTER (WHERE l.status='closed' AND l.quality_r>0),0)>0 THEN 999
                WHEN ABS(COALESCE(SUM(l.quality_r) FILTER (WHERE l.status='closed' AND l.quality_r<0),0))=0 THEN 0
                ELSE ROUND(
                    COALESCE(SUM(l.quality_r) FILTER (WHERE l.status='closed' AND l.quality_r>0),0)
                    / ABS(SUM(l.quality_r) FILTER (WHERE l.status='closed' AND l.quality_r<0)),3
                )
            END AS profit_factor
        FROM shadow_trade_legs l
        JOIN shadow_trades t ON t.id=l.shadow_trade_id
        JOIN sources s ON s.id=t.source_id
        WHERE t.score_eligible
          AND t.benchmark_model='fixed_1000_10_per_tp_fair_v2'
        GROUP BY t.source_id,COALESCE(s.chat_title,s.source_alias),t.provider_style,t.side,t.session_bucket,t.weekday_iso,l.tp_index
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_benchmark_segments")
    op.execute("DROP VIEW IF EXISTS provider_benchmark_performance")
    op.drop_table("shadow_trade_legs")
    for constraint in (
        "ck_shadow_fair_target_count",
        "ck_shadow_fair_quote_mode",
        "ck_shadow_fair_weekday",
        "ck_shadow_fair_readiness",
        "ck_shadow_fair_style",
    ):
        op.drop_constraint(constraint, "shadow_trades", type_="check")
    for column in (
        "target_count","entry_spread","entry_delay_ms","score_exclusion_reason","score_eligible",
        "quote_mode","weekday_iso","session_bucket","signal_posted_at","interpretation_readiness_at_entry","provider_style",
    ):
        op.drop_column("shadow_trades", column)
    op.drop_constraint("ck_shadow_benchmark_model", "shadow_trades", type_="check")
    op.alter_column("shadow_trades", "benchmark_model", server_default="fixed_1000_10_per_tp_v1")
    op.execute("UPDATE shadow_trades SET benchmark_model='fixed_1000_10_per_tp_v1'")
    op.create_check_constraint("ck_shadow_benchmark_model", "shadow_trades", "benchmark_model IN ('fixed_1000_10_per_tp_v1')")
    from services.api.migrations.versions import _dummy  # pragma: no cover
