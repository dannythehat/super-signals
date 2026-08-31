"""Add normalized Provider Lab benchmark performance view.

Revision ID: 0046_provider_benchmark_view
Revises: 0045_provider_benchmark_usd
Create Date: 2026-08-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0046_provider_benchmark_view"
down_revision: str | None = "0045_provider_benchmark_usd"
branch_labels: str | Sequence[str] | None = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIEW provider_benchmark_performance AS
        WITH closed AS (
            SELECT
                t.id,
                t.source_id,
                t.closed_at,
                t.quality_r_multiple,
                t.benchmark_pnl_usd,
                SUM(t.benchmark_pnl_usd) OVER (
                    PARTITION BY t.source_id
                    ORDER BY t.closed_at, t.id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS cumulative_pnl_usd
            FROM shadow_trades t
            WHERE t.status='closed'
              AND t.closed_at IS NOT NULL
              AND t.benchmark_model='fixed_1000_10_per_tp_v1'
        ), equity AS (
            SELECT
                c.*,
                MAX(c.cumulative_pnl_usd) OVER (
                    PARTITION BY c.source_id
                    ORDER BY c.closed_at, c.id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS running_peak_pnl_usd
            FROM closed c
        ), agg AS (
            SELECT
                source_id,
                COUNT(*)::integer AS closed_trades,
                COUNT(*) FILTER (WHERE quality_r_multiple > 0)::integer AS wins,
                COUNT(*) FILTER (WHERE quality_r_multiple < 0)::integer AS losses,
                COUNT(DISTINCT closed_at::date)::integer AS trading_days,
                COALESCE(SUM(quality_r_multiple),0)::numeric(18,6) AS total_r,
                COALESCE(AVG(quality_r_multiple),0)::numeric(18,6) AS average_r,
                COALESCE(SUM(benchmark_pnl_usd),0)::numeric(18,2) AS benchmark_pnl_usd,
                (1000 + COALESCE(SUM(benchmark_pnl_usd),0))::numeric(18,2) AS benchmark_balance_usd,
                COALESCE(MAX(running_peak_pnl_usd - cumulative_pnl_usd),0)::numeric(18,2) AS max_drawdown_usd,
                COALESCE(
                    SUM(quality_r_multiple) FILTER (WHERE quality_r_multiple > 0),0
                )::numeric(18,6) AS gross_positive_r,
                ABS(COALESCE(
                    SUM(quality_r_multiple) FILTER (WHERE quality_r_multiple < 0),0
                ))::numeric(18,6) AS gross_negative_r
            FROM equity
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
            COALESCE(a.wins,0) AS wins,
            COALESCE(a.losses,0) AS losses,
            COALESCE(a.trading_days,0) AS trading_days,
            CASE WHEN COALESCE(a.closed_trades,0) > 0
                 THEN ROUND((COALESCE(a.wins,0)::numeric / a.closed_trades) * 100,2)
                 ELSE 0 END AS win_rate_percent,
            COALESCE(a.total_r,0)::numeric(18,6) AS total_r,
            COALESCE(a.average_r,0)::numeric(18,6) AS average_r,
            CASE
                WHEN COALESCE(a.gross_negative_r,0)=0 AND COALESCE(a.gross_positive_r,0)>0 THEN 999
                WHEN COALESCE(a.gross_negative_r,0)=0 THEN 0
                ELSE ROUND(a.gross_positive_r / a.gross_negative_r,3)
            END AS profit_factor,
            COALESCE(a.benchmark_pnl_usd,0)::numeric(18,2) AS benchmark_pnl_usd,
            COALESCE(a.benchmark_balance_usd,1000)::numeric(18,2) AS benchmark_balance_usd,
            ROUND((COALESCE(a.benchmark_pnl_usd,0) / 1000) * 100,2) AS benchmark_return_percent,
            COALESCE(a.max_drawdown_usd,0)::numeric(18,2) AS max_drawdown_usd,
            CASE
                WHEN p.duplicate_of_source_id IS NOT NULL THEN 'DUPLICATE_REVIEW'
                WHEN COALESCE(p.interpretation_readiness,0) < 0.90 THEN 'LEARNING'
                WHEN COALESCE(p.style,'unknown')='scalper'
                     AND (COALESCE(a.closed_trades,0) < 100 OR COALESCE(a.trading_days,0) < 20)
                     THEN 'INSUFFICIENT_EVIDENCE'
                WHEN COALESCE(p.style,'unknown')='intraday'
                     AND (COALESCE(a.closed_trades,0) < 60 OR COALESCE(a.trading_days,0) < 30)
                     THEN 'INSUFFICIENT_EVIDENCE'
                WHEN COALESCE(p.style,'unknown')='swing_or_sparse'
                     AND (COALESCE(a.closed_trades,0) < 30 OR COALESCE(a.trading_days,0) < 45)
                     THEN 'INSUFFICIENT_EVIDENCE'
                WHEN COALESCE(p.style,'unknown') IN ('mixed','unknown')
                     AND (COALESCE(a.closed_trades,0) < 60 OR COALESCE(a.trading_days,0) < 30)
                     THEN 'INSUFFICIENT_EVIDENCE'
                WHEN COALESCE(a.total_r,0) > 0
                     AND (CASE
                         WHEN COALESCE(a.gross_negative_r,0)=0 AND COALESCE(a.gross_positive_r,0)>0 THEN 999
                         WHEN COALESCE(a.gross_negative_r,0)=0 THEN 0
                         ELSE a.gross_positive_r / a.gross_negative_r END) >= 1.30
                     AND COALESCE(a.average_r,0) > 0
                     AND COALESCE(a.max_drawdown_usd,0) <= 150
                     AND COALESCE(p.interpretation_readiness,0) >= 0.95
                     THEN 'QUALIFIED_EVIDENCE'
                WHEN COALESCE(a.total_r,0) > 0 THEN 'PROMISING'
                ELSE 'NOT_PROVEN'
            END AS evidence_status
        FROM sources s
        LEFT JOIN provider_research_profiles p ON p.source_id=s.id
        LEFT JOIN agg a ON a.source_id=s.id
        WHERE s.status <> 'revoked'
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_benchmark_performance")
